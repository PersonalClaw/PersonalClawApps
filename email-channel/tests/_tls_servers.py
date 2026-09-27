"""Loopback mail servers that complete a REAL TLS handshake, for the certificate tests.

Named ``_tls_servers`` (leading underscore, not ``test_*``) so pytest doesn't collect it. No
``personalclaw`` import: the apps boundary lint does not skip this file.

A throwaway certificate authority signs a server certificate (``mint``), and two tiny
servers bound to 127.0.0.1 on an ephemeral port speak just enough IMAP and SMTP for a login:
``ImapsServer`` (implicit TLS) and ``StarttlsSmtpServer`` (plaintext, then STARTTLS). Each
records the logins it received, which is how a test proves a refused certificate never saw
the password. Nothing leaves the machine.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import socket
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class Minted:
    ca: Path
    cert: Path
    key: Path


def mint(directory: Path, *, names: tuple[str, ...] = ("127.0.0.1",)) -> Minted:
    """A CA, and a server certificate it signed for ``names`` (IP addresses or DNS names)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test mail CA")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        # Python's default context verifies with X509_STRICT, which needs both key ids.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    paths = Minted(directory / "ca.pem", directory / "server.pem", directory / "server.key")
    paths.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths.key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


class _Server:
    """One loopback listener; a thread answers each connection in turn."""

    def __init__(self, minted: Minted) -> None:
        self.logins: list[str] = []
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(minted.cert, minted.key)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        # A short accept timeout and a stop flag, because closing a listening socket does not
        # wake a thread blocked in accept() on every platform.
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            raw.settimeout(10)
            try:
                self.handle(raw)
            except (OSError, ssl.SSLError):
                pass  # the client refused the certificate, or hung up: the next one may not
            finally:
                raw.close()

    def handle(self, raw: socket.socket) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()


class ImapsServer(_Server):
    """IMAP over implicit TLS: greeting, CAPABILITY, LOGIN, SELECT, STATUS, LOGOUT."""

    def handle(self, raw: socket.socket) -> None:
        conn = self._context.wrap_socket(raw, server_side=True)
        stream = conn.makefile("rwb")
        stream.write(b"* OK test IMAP ready\r\n")
        stream.flush()
        for line in stream:
            tag, _, rest = line.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            command = rest.split(" ", 1)[0].upper()
            if command == "CAPABILITY":
                stream.write(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n")
            elif command == "LOGIN":
                self.logins.append(rest[len("LOGIN "):])
            elif command in ("SELECT", "EXAMINE"):
                stream.write(b"* 0 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n")
            elif command == "STATUS":
                stream.write(b'* STATUS "INBOX" (UIDVALIDITY 7)\r\n')
            elif command == "LOGOUT":
                stream.write(b"* BYE bye\r\n" + f"{tag} OK done\r\n".encode())
                stream.flush()
                return
            stream.write(f"{tag} OK done\r\n".encode())
            stream.flush()


class StarttlsSmtpServer(_Server):
    """SMTP submission: EHLO, STARTTLS, EHLO again, AUTH PLAIN, QUIT."""

    def handle(self, raw: socket.socket) -> None:
        stream = raw.makefile("rwb")
        stream.write(b"220 test SMTP ready\r\n")
        stream.flush()
        tls = None
        while True:
            line = stream.readline()
            if not line:
                return
            verb = line.decode("utf-8", "replace").strip().split(" ", 1)
            command = verb[0].upper()
            if command == "EHLO":
                offer = b"250 AUTH PLAIN\r\n" if tls else b"250 STARTTLS\r\n"
                stream.write(b"250-test\r\n" + offer)
            elif command == "STARTTLS":
                stream.write(b"220 go ahead\r\n")
                stream.flush()
                tls = self._context.wrap_socket(raw, server_side=True)
                stream = tls.makefile("rwb")
                continue
            elif command == "AUTH":
                user = base64.b64decode(verb[1].split(" ", 1)[1]).split(b"\0")[1].decode()
                self.logins.append(user)
                stream.write(b"235 ok\r\n")
            elif command == "QUIT":
                stream.write(b"221 bye\r\n")
                stream.flush()
                return
            else:
                stream.write(b"250 ok\r\n")
            stream.flush()
