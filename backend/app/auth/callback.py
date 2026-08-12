"""Transient HTTPS listener that catches Schwab's OAuth redirect.

Schwab requires an HTTPS callback URL; https://127.0.0.1 is allowed for local
dev. We generate a self-signed cert once (stored in the data dir), run a tiny
TLS server on the callback port ONLY while an auth flow is in progress, capture
?code=..., hand it to AuthManager, and shut down. The browser shows a one-time
self-signed-cert warning — expected; the request never leaves the machine."""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
import urllib.parse
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ..logging import get_logger

log = get_logger("oauth-callback")

_OK_HTML = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
    b"<html><body style='font-family:sans-serif;background:#0b0e14;color:#ddd'>"
    b"<h2>Schwab connected \xe2\x9c\x93</h2><p>You can close this tab and return "
    b"to Autotrader.</p></body></html>"
)
_ERR_HTML = (
    b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
    b"<html><body><h2>No authorization code found in the redirect.</h2></body></html>"
)


def ensure_self_signed_cert(data_dir: Path) -> tuple[Path, Path]:
    """Create (once) and return paths to a self-signed cert/key for 127.0.0.1."""
    cert_path, key_path = data_dir / "callback-cert.pem", data_dir / "callback-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("self_signed_cert_created", path=str(cert_path))
    return cert_path, key_path


async def wait_for_code(data_dir: Path, port: int, timeout_s: float = 300) -> str:
    """Run the TLS listener until one redirect with ?code= arrives (or timeout)."""
    cert_path, key_path = ensure_self_signed_cert(data_dir)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)

    code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            # e.g. b"GET /oauth/callback?code=...&session=... HTTP/1.1"
            parts = request_line.decode(errors="replace").split(" ")
            path = parts[1] if len(parts) >= 2 else ""
            query = urllib.parse.urlparse(path).query
            code = urllib.parse.parse_qs(query).get("code", [None])[0]
            if code and not code_future.done():
                writer.write(_OK_HTML)
                code_future.set_result(code)
            else:
                writer.write(_ERR_HTML)
            await writer.drain()
        except Exception as e:  # noqa: BLE001 — never let one bad request kill the flow
            log.warning("callback_request_error", error=str(e))
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=port, ssl=ctx)
    log.info("callback_listener_started", port=port)
    try:
        return await asyncio.wait_for(code_future, timeout=timeout_s)
    finally:
        server.close()
        await server.wait_closed()
        log.info("callback_listener_stopped")
