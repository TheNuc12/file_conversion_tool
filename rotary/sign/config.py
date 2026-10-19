"""Settings for the signing server.

Read from ``rotary-sign.toml`` in the data folder, then overridden by
``ROTARY_SIGN_*`` environment variables (handy for Docker). Every setting has
a sensible default except ``url``, which emails need to link back correctly.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    data: Path = Path("rotary-sign-data")  # database, documents, seal certificate
    url: str = "http://localhost:8080"  # how people reach the server; used in email links
    organisation: str = "Our company"
    # Email. With no smtp_host, messages are saved to <data>/outbox and the sender can
    # copy each signing link from the envelope page instead.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_security: str = "starttls"  # starttls | ssl | none
    mail_from: str = ""
    # Sealing. Without a certificate, a self-signed one is generated on first run.
    seal_p12: str = ""
    seal_password: str = ""
    timestamp_url: str = ""  # optional RFC 3161 timestamp authority
    link_days: int = 30  # signing links expire after this many days
    max_upload_mb: int = 25
    trust_proxy: bool = False  # honour X-Forwarded-For/Proto from a reverse proxy
    secret_key: str = ""  # session signing key; generated and stored if empty

    @property
    def secure_cookies(self) -> bool:
        return self.url.startswith("https://")

    @property
    def sender_address(self) -> str:
        return self.mail_from or self.smtp_user or "rotary-sign@localhost"


def _coerce(kind, value):
    if kind is bool or kind == "bool":
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind is int or kind == "int":
        return int(value)
    if kind is Path or kind == "Path":
        return Path(value)
    return str(value)


def load(data_dir: str | os.PathLike | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    data = Path(data_dir or env.get("ROTARY_SIGN_DATA") or cfg.data)
    values: dict = {}
    config_file = data / "rotary-sign.toml"
    if config_file.exists():
        with open(config_file, "rb") as f:
            values.update(tomllib.load(f))
    for f in fields(Config):
        key = f"ROTARY_SIGN_{f.name.upper()}"
        if key in env:
            values[f.name] = env[key]
    for f in fields(Config):
        if f.name in values and f.name != "data":
            setattr(cfg, f.name, _coerce(f.type, values[f.name]))
    cfg.data = data
    cfg.url = cfg.url.rstrip("/")
    if cfg.smtp_security not in ("starttls", "ssl", "none"):
        raise ValueError("smtp_security must be starttls, ssl or none")
    return cfg
