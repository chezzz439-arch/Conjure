"""Cloud auth, per-user model library, and Conjure device pairing.

Talks to InsForge when the backend is reachable. Falls back to a local SQLite
mirror in conjure.db so Studio / pairing still works offline (e.g. InsForge 503).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("conjure.cloud")

AUTH_COOKIE = "conjure_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 14  # 14 days
PAIRING_TTL_SEC = 15 * 60


class CloudStore:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bucket: str,
        db_path: Path,
        secret: str,
        device_state_path: Path,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.bucket = bucket or "conjure-models"
        self.db_path = Path(db_path)
        self.secret = secret or "conjure-dev-secret"
        self.device_state_path = Path(device_state_path)
        self._lock = threading.Lock()
        self._insforge_ok: bool | None = None
        self._ensure_local_schema()

    # ── local schema ───────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _ensure_local_schema(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS cloud_users (
                  id TEXT PRIMARY KEY,
                  email TEXT UNIQUE NOT NULL,
                  name TEXT,
                  password_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_models (
                  id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  prompt TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT 'meshy',
                  glb_url TEXT,
                  stl_url TEXT,
                  glb_key TEXT,
                  stl_key TEXT,
                  local_model_id TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS user_models_user_idx ON user_models(user_id);
                CREATE TABLE IF NOT EXISTS devices (
                  id TEXT PRIMARY KEY,
                  user_id TEXT,
                  name TEXT NOT NULL DEFAULT 'Conjure Kiosk',
                  device_secret_hash TEXT NOT NULL,
                  pairing_code TEXT,
                  pairing_expires_at TEXT,
                  last_seen_at TEXT,
                  base_url TEXT,
                  status TEXT NOT NULL DEFAULT 'offline',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS devices_user_idx ON devices(user_id);
                CREATE INDEX IF NOT EXISTS devices_pair_idx ON devices(pairing_code);
                CREATE TABLE IF NOT EXISTS device_inbox (
                  id TEXT PRIMARY KEY,
                  device_id TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL,
                  claimed_at TEXT
                );
                """
            )

    # ── crypto helpers ─────────────────────────────────────────────────────
    def _pw_hash(self, password: str, salt: str | None = None) -> str:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
        return f"{salt}${digest.hex()}"

    def _pw_ok(self, password: str, stored: str) -> bool:
        try:
            salt, _ = stored.split("$", 1)
        except ValueError:
            return False
        return hmac.compare_digest(self._pw_hash(password, salt), stored)

    def _sign(self, payload: str) -> str:
        sig = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def _unsign(self, token: str) -> str | None:
        if not token or "." not in token:
            return None
        # Starlette may quote cookie values; strip wrapping quotes.
        token = token.strip().strip('"')
        payload, sig = token.rsplit(".", 1)
        expect = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        return payload

    def mint_session(self, user_id: str, email: str, name: str = "") -> str:
        import base64
        payload = json.dumps(
            {
                "uid": user_id,
                "email": email,
                "name": name or "",
                "exp": int(time.time()) + COOKIE_MAX_AGE,
            },
            separators=(",", ":"),
        )
        raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return self._sign(raw)

    def user_from_token(self, token: str | None) -> dict | None:
        import base64
        payload = self._unsign(token or "")
        if not payload:
            return None
        data = None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            try:
                pad = "=" * (-len(payload) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload + pad))
            except Exception:
                return None
        if not isinstance(data, dict):
            return None
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return {"id": data["uid"], "email": data.get("email", ""), "name": data.get("name", "")}

    # ── InsForge reachability ──────────────────────────────────────────────
    def insforge_available(self) -> bool:
        if not self.base_url or not self.api_key:
            self._insforge_ok = False
            return False
        if self._insforge_ok is not None and self._insforge_ok:
            return True
        try:
            r = requests.get(
                f"{self.base_url}/api/health",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=3,
            )
            # 404 still means the host is up; 503 means no backend services.
            ok = r.status_code < 500
            self._insforge_ok = ok
            return ok
        except Exception:
            self._insforge_ok = False
            return False

    def _if_headers(self, user_token: str | None = None) -> dict:
        token = user_token or self.api_key
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ── auth ───────────────────────────────────────────────────────────────
    def sign_up(self, email: str, password: str, name: str = "") -> dict:
        email = email.strip().lower()
        if not email or len(password) < 6:
            raise ValueError("Email and password (6+ chars) required")

        if self.insforge_available():
            try:
                r = requests.post(
                    f"{self.base_url}/api/auth/users",
                    json={"email": email, "password": password, "name": name or None},
                    timeout=20,
                )
                body = r.json() if r.content else {}
                if r.status_code in (200, 201):
                    user = body.get("user") or body.get("data", {}).get("user") or body
                    uid = user.get("id") or body.get("id")
                    if uid:
                        # Auto sign-in when token returned; else sign in.
                        token = body.get("accessToken") or (body.get("data") or {}).get("accessToken")
                        if not token:
                            return self.sign_in(email, password)
                        session = self.mint_session(uid, email, name or user.get("name") or "")
                        return {
                            "user": {"id": uid, "email": email, "name": name},
                            "session": session,
                            "backend": "insforge",
                        }
                # Email already registered → try local mirror path below
                if r.status_code not in (409, 400):
                    log.warning("InsForge signup HTTP %s: %s", r.status_code, str(body)[:200])
            except Exception as e:
                log.warning("InsForge signup failed: %s", e)
                self._insforge_ok = False

        uid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            existing = c.execute("SELECT id FROM cloud_users WHERE email=?", (email,)).fetchone()
            if existing:
                raise ValueError("An account with that email already exists")
            c.execute(
                "INSERT INTO cloud_users (id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                (uid, email, name, self._pw_hash(password), now),
            )
        return {
            "user": {"id": uid, "email": email, "name": name},
            "session": self.mint_session(uid, email, name),
            "backend": "local",
        }

    def sign_in(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        if not email or not password:
            raise ValueError("Email and password required")

        if self.insforge_available():
            try:
                r = requests.post(
                    f"{self.base_url}/api/auth/sessions?client_type=server",
                    json={"email": email, "password": password},
                    timeout=20,
                )
                body = r.json() if r.content else {}
                if r.status_code == 200:
                    user = body.get("user") or (body.get("data") or {}).get("user") or {}
                    uid = user.get("id") or body.get("userId")
                    if uid:
                        return {
                            "user": {
                                "id": uid,
                                "email": user.get("email") or email,
                                "name": (user.get("profile") or {}).get("name")
                                or user.get("name")
                                or "",
                            },
                            "session": self.mint_session(
                                uid,
                                user.get("email") or email,
                                (user.get("profile") or {}).get("name") or user.get("name") or "",
                            ),
                            "accessToken": body.get("accessToken"),
                            "backend": "insforge",
                        }
                if r.status_code == 401:
                    pass  # fall through to local
                else:
                    log.warning("InsForge signin HTTP %s: %s", r.status_code, str(body)[:200])
            except Exception as e:
                log.warning("InsForge signin failed: %s", e)
                self._insforge_ok = False

        with self._conn() as c:
            row = c.execute("SELECT * FROM cloud_users WHERE email=?", (email,)).fetchone()
        if not row or not self._pw_ok(password, row["password_hash"]):
            raise ValueError("Invalid email or password")
        return {
            "user": {"id": row["id"], "email": row["email"], "name": row["name"] or ""},
            "session": self.mint_session(row["id"], row["email"], row["name"] or ""),
            "backend": "local",
        }

    # ── storage ────────────────────────────────────────────────────────────
    def upload_user_file(
        self, file_path: Path, user_id: str, filename: str
    ) -> tuple[str | None, str | None]:
        """Returns (url, key). Prefers InsForge; mirrors to local path URL otherwise."""
        key = f"users/{user_id}/{filename}"
        if self.insforge_available() and self.api_key and file_path.exists():
            try:
                with open(file_path, "rb") as f:
                    r = requests.post(
                        f"{self.base_url}/api/storage/buckets/{self.bucket}/objects",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        files={"file": (key, f, "application/octet-stream")},
                        data={"path": key},
                        timeout=60,
                    )
                if r.status_code in (200, 201):
                    body = r.json() if r.content else {}
                    out_key = body.get("key") or key
                    url = body.get("url") or (
                        f"{self.base_url}/api/storage/buckets/{self.bucket}/objects/{out_key}"
                    )
                    return url, out_key
                log.warning("InsForge user upload HTTP %s: %s", r.status_code, r.text[:160])
            except Exception as e:
                log.warning("InsForge user upload failed: %s", e)

        # Local fallback: copy under output/cloud/{user_id}/
        dest_dir = self.device_state_path.parent / "cloud" / user_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(filename).name
        try:
            import shutil

            shutil.copy2(file_path, dest)
            return f"/api/cloud/files/{user_id}/{dest.name}", key
        except Exception as e:
            log.warning("Local cloud file copy failed: %s", e)
            return None, None

    # ── user models ────────────────────────────────────────────────────────
    def save_user_model(
        self,
        user_id: str,
        *,
        prompt: str,
        source: str,
        glb_path: Path | None,
        stl_path: Path | None,
        local_model_id: str | None = None,
    ) -> dict | None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        mid = str(uuid.uuid4())
        glb_url = glb_key = stl_url = stl_key = None
        if glb_path and Path(glb_path).exists():
            glb_url, glb_key = self.upload_user_file(Path(glb_path), user_id, f"{stamp}-model.glb")
        if stl_path and Path(stl_path).exists():
            stl_url, stl_key = self.upload_user_file(Path(stl_path), user_id, f"{stamp}-model.stl")
        if not glb_url and not stl_url:
            return None

        row = {
            "id": mid,
            "user_id": user_id,
            "prompt": prompt or "",
            "source": source or "meshy",
            "glb_url": glb_url,
            "stl_url": stl_url,
            "glb_key": glb_key,
            "stl_key": stl_key,
            "local_model_id": str(local_model_id) if local_model_id else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        if self.insforge_available():
            try:
                r = requests.post(
                    f"{self.base_url}/api/database/records/user_models",
                    headers=self._if_headers(),
                    json=[row],
                    timeout=20,
                )
                if r.status_code in (200, 201):
                    data = r.json()
                    if isinstance(data, list) and data:
                        row = {**row, **data[0]}
                    log.info("Cloud model saved to InsForge: %s", mid)
                else:
                    log.warning("InsForge user_models insert HTTP %s: %s", r.status_code, r.text[:160])
            except Exception as e:
                log.warning("InsForge user_models insert failed: %s", e)

        with self._lock, self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO user_models
                   (id, user_id, prompt, source, glb_url, stl_url, glb_key, stl_key, local_model_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"],
                    row["user_id"],
                    row["prompt"],
                    row["source"],
                    row["glb_url"],
                    row["stl_url"],
                    row["glb_key"],
                    row["stl_key"],
                    row["local_model_id"],
                    row["created_at"],
                ),
            )
        return row

    def list_user_models(self, user_id: str) -> list[dict]:
        models: list[dict] = []
        if self.insforge_available():
            try:
                r = requests.get(
                    f"{self.base_url}/api/database/records/user_models",
                    headers=self._if_headers(),
                    params={"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": "100"},
                    timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        models = data
            except Exception as e:
                log.warning("InsForge list models failed: %s", e)

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM user_models WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                (user_id,),
            ).fetchall()
        local = [dict(r) for r in rows]
        if not models:
            return local
        # Merge by id, prefer InsForge rows.
        by_id = {m["id"]: m for m in local}
        for m in models:
            by_id[m["id"]] = m
        return sorted(by_id.values(), key=lambda x: x.get("created_at") or "", reverse=True)

    def get_user_model(self, user_id: str, model_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM user_models WHERE id=? AND user_id=?",
                (model_id, user_id),
            ).fetchone()
        if row:
            return dict(row)
        if self.insforge_available():
            try:
                r = requests.get(
                    f"{self.base_url}/api/database/records/user_models",
                    headers=self._if_headers(),
                    params={"id": f"eq.{model_id}", "user_id": f"eq.{user_id}", "limit": "1"},
                    timeout=15,
                )
                if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                    return r.json()[0]
            except Exception as e:
                log.warning("InsForge get model failed: %s", e)
        return None

    # ── devices ────────────────────────────────────────────────────────────
    def _hash_secret(self, secret: str) -> str:
        return hashlib.sha256(f"{self.secret}:{secret}".encode()).hexdigest()

    def load_local_device(self) -> dict | None:
        if not self.device_state_path.exists():
            return None
        try:
            return json.loads(self.device_state_path.read_text())
        except Exception:
            return None

    def save_local_device(self, data: dict) -> None:
        self.device_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.device_state_path.write_text(json.dumps(data, indent=2))

    def register_device(self, *, name: str = "Conjure Kiosk", base_url: str = "") -> dict:
        """Create or refresh pairing code for this kiosk."""
        existing = self.load_local_device()
        device_id = (existing or {}).get("id") or str(uuid.uuid4())
        secret = (existing or {}).get("secret") or secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = (datetime.now(timezone.utc) + timedelta(seconds=PAIRING_TTL_SEC)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        secret_hash = self._hash_secret(secret)

        with self._lock, self._conn() as c:
            row = c.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
            if row:
                c.execute(
                    """UPDATE devices SET name=?, device_secret_hash=?, pairing_code=?,
                       pairing_expires_at=?, base_url=?, status='pairing', last_seen_at=?
                       WHERE id=?""",
                    (name, secret_hash, code, expires, base_url, now, device_id),
                )
            else:
                c.execute(
                    """INSERT INTO devices
                       (id, user_id, name, device_secret_hash, pairing_code, pairing_expires_at,
                        last_seen_at, base_url, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (device_id, None, name, secret_hash, code, expires, now, base_url, "pairing", now),
                )

        if self.insforge_available():
            payload = {
                "id": device_id,
                "name": name,
                "device_secret_hash": secret_hash,
                "pairing_code": code,
                "pairing_expires_at": expires,
                "base_url": base_url or None,
                "status": "pairing",
                "last_seen_at": now,
            }
            try:
                # Prefer upsert
                r = requests.post(
                    f"{self.base_url}/api/database/records/devices",
                    headers={
                        **self._if_headers(),
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=[payload],
                    timeout=15,
                )
                if r.status_code not in (200, 201):
                    log.warning("InsForge device register HTTP %s: %s", r.status_code, r.text[:160])
            except Exception as e:
                log.warning("InsForge device register failed: %s", e)

        state = {"id": device_id, "secret": secret, "name": name}
        self.save_local_device(state)
        return {
            "device_id": device_id,
            "pairing_code": code,
            "expires_at": expires,
            "name": name,
        }

    def claim_device(self, user_id: str, pairing_code: str, name: str | None = None) -> dict:
        code = (pairing_code or "").strip().replace(" ", "")
        if not code:
            raise ValueError("Pairing code required")
        now = datetime.now(timezone.utc)
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM devices WHERE pairing_code=?",
                (code,),
            ).fetchone()
            if not row:
                raise ValueError("Invalid pairing code")
            exp = row["pairing_expires_at"]
            if exp:
                try:
                    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    if exp_dt < now:
                        raise ValueError("Pairing code expired — generate a new one on the kiosk")
                except ValueError as e:
                    if "expired" in str(e):
                        raise
            c.execute(
                """UPDATE devices SET user_id=?, name=COALESCE(?, name), pairing_code=NULL,
                   pairing_expires_at=NULL, status='online', last_seen_at=? WHERE id=?""",
                (user_id, name, now.isoformat(), row["id"]),
            )
            out = dict(row)
            out["user_id"] = user_id
            out["status"] = "online"
            out["name"] = name or row["name"]

        if self.insforge_available():
            try:
                requests.patch(
                    f"{self.base_url}/api/database/records/devices",
                    headers=self._if_headers(),
                    params={"id": f"eq.{row['id']}"},
                    json={
                        "user_id": user_id,
                        "name": out["name"],
                        "pairing_code": None,
                        "pairing_expires_at": None,
                        "status": "online",
                        "last_seen_at": now.isoformat(),
                    },
                    timeout=15,
                )
            except Exception as e:
                log.warning("InsForge claim patch failed: %s", e)
        return {
            "id": out["id"],
            "name": out["name"],
            "status": "online",
            "base_url": out.get("base_url"),
        }

    def list_devices(self, user_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, status, last_seen_at, base_url, created_at FROM devices WHERE user_id=? ORDER BY last_seen_at DESC",
                (user_id,),
            ).fetchall()
        devices = [dict(r) for r in rows]
        # Mark stale as offline
        now = datetime.now(timezone.utc)
        for d in devices:
            ls = d.get("last_seen_at")
            online = False
            if ls:
                try:
                    ls_dt = datetime.fromisoformat(ls.replace("Z", "+00:00"))
                    online = (now - ls_dt).total_seconds() < 90
                except Exception:
                    pass
            d["status"] = "online" if online else "offline"
        return devices

    def heartbeat(self, device_id: str, device_secret: str, base_url: str = "") -> dict:
        expect = self._hash_secret(device_secret)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not row or not hmac.compare_digest(row["device_secret_hash"], expect):
                raise ValueError("Unknown device")
            c.execute(
                "UPDATE devices SET last_seen_at=?, status='online', base_url=COALESCE(NULLIF(?, ''), base_url) WHERE id=?",
                (now, base_url, device_id),
            )
        return {"ok": True, "last_seen_at": now}

    def push_model_to_device(self, user_id: str, device_id: str, model_id: str) -> dict:
        devices = {d["id"]: d for d in self.list_devices(user_id)}
        if device_id not in devices:
            # device may be online with stale list status — check ownership
            with self._conn() as c:
                row = c.execute(
                    "SELECT id FROM devices WHERE id=? AND user_id=?",
                    (device_id, user_id),
                ).fetchone()
            if not row:
                raise ValueError("Device not found")
        model = self.get_user_model(user_id, model_id)
        if not model:
            raise ValueError("Model not found")
        inbox_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO device_inbox (id, device_id, model_id, status, created_at)
                   VALUES (?,?,?,?,?)""",
                (inbox_id, device_id, model_id, "pending", now),
            )
        return {"id": inbox_id, "device_id": device_id, "model_id": model_id, "status": "pending"}

    def poll_inbox(self, device_id: str, device_secret: str) -> list[dict]:
        expect = self._hash_secret(device_secret)
        with self._conn() as c:
            row = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not row or not hmac.compare_digest(row["device_secret_hash"], expect):
                raise ValueError("Unknown device")
            pending = c.execute(
                """SELECT i.*, m.prompt, m.source, m.glb_url, m.stl_url, m.glb_key, m.stl_key
                   FROM device_inbox i
                   JOIN user_models m ON m.id = i.model_id
                   WHERE i.device_id=? AND i.status='pending'
                   ORDER BY i.created_at ASC""",
                (device_id,),
            ).fetchall()
        return [dict(r) for r in pending]

    def claim_inbox_item(self, device_id: str, device_secret: str, inbox_id: str) -> dict:
        expect = self._hash_secret(device_secret)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not row or not hmac.compare_digest(row["device_secret_hash"], expect):
                raise ValueError("Unknown device")
            item = c.execute(
                "SELECT * FROM device_inbox WHERE id=? AND device_id=?",
                (inbox_id, device_id),
            ).fetchone()
            if not item:
                raise ValueError("Inbox item not found")
            c.execute(
                "UPDATE device_inbox SET status='delivered', claimed_at=? WHERE id=?",
                (now, inbox_id),
            )
            model = c.execute(
                "SELECT * FROM user_models WHERE id=?",
                (item["model_id"],),
            ).fetchone()
        return {"inbox": dict(item), "model": dict(model) if model else None}


def build_cloud_store(
    *,
    base_url: str,
    api_key: str,
    bucket: str,
    db_path: Path,
    output_dir: Path,
) -> CloudStore:
    secret = os.getenv("CONJURE_SESSION_SECRET") or api_key or "conjure-dev-secret"
    return CloudStore(
        base_url=base_url,
        api_key=api_key,
        bucket=bucket,
        db_path=db_path,
        secret=secret,
        device_state_path=output_dir / "device.json",
    )
