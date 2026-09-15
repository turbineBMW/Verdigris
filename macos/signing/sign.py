#!/usr/bin/env python3
"""Sign local Verdigris Mac builds with a persistent, private signing identity.

This identity is only for local builds; it does not claim Apple Developer ID or
notarization. Authorize it for code signing in the user's trust settings once;
it is not installed as a system/web trust anchor.
"""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile


def run(*argv):
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Signing command failed")
    return result.stdout


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("app", nargs="?", type=Path)
args = parser.parse_args()
root = Path.home() / "Library/Application Support/VerdigrisSigning"
root.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(root, 0o700)
keychain = root / "local-builds.keychain-db"
password_path = root / "keychain-password"
certificate = root / "local-builds.der"
os.umask(0o077)
if not keychain.exists():
    password = secrets.token_urlsafe(48)
    password_path.write_text(password)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        temporary = Path(temporary)
        config = temporary / "certificate.cnf"
        config.write_text("""[req]
distinguished_name=dn
x509_extensions=extensions
prompt=no
[dn]
CN=Verdigris Local Builds
[extensions]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
subjectKeyIdentifier=hash
""")
        run("openssl", "req", "-new", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650",
            "-config", str(config), "-keyout", str(temporary / "key.pem"), "-out", str(temporary / "cert.pem"))
        run("openssl", "x509", "-in", str(temporary / "cert.pem"), "-outform", "DER", "-out", str(certificate))
        envfile = temporary / "p12-password"
        envfile.write_text(secrets.token_urlsafe(32))
        run("openssl", "pkcs12", "-export", "-inkey", str(temporary / "key.pem"), "-in", str(temporary / "cert.pem"),
            "-out", str(temporary / "identity.p12"), "-passout", "file:" + str(envfile))
        run("security", "create-keychain", "-p", password, str(keychain))
        run("security", "unlock-keychain", "-p", password, str(keychain))
        run("security", "import", str(temporary / "identity.p12"), "-k", str(keychain),
            "-P", envfile.read_text(), "-T", "/usr/bin/codesign")
        run("security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", password, str(keychain))
if not password_path.exists() or not certificate.exists():
    raise SystemExit("Local signing identity setup is incomplete; preserve it and inspect before continuing")
identity = hashlib.sha1(certificate.read_bytes()).hexdigest().upper()
# codesign's key lookup also needs this keychain in the user search list on
# current macOS, even with --keychain. Preserve all existing entries and order.
keychains = shlex.split(run("security", "list-keychains", "-d", "user"))
if str(keychain) not in keychains:
    run("security", "list-keychains", "-d", "user", "-s", *keychains, str(keychain))
if args.app:
    import plistlib
    with (args.app / "Contents/Info.plist").open("rb") as handle:
        identifier = plistlib.load(handle)["CFBundleIdentifier"]
    if identifier != "com.cleverdevil.iCloudBridge":
        raise SystemExit("This signing script is scoped to iCloudBridge")
    run("security", "unlock-keychain", "-p", password_path.read_text(), str(keychain))
    requirement = f'designated => identifier "{identifier}" and certificate leaf = H"{identity}"'
    run("codesign", "--force", "--sign", identity, "--keychain", str(keychain),
        "--timestamp=none", "--requirements", "=" + requirement, str(args.app))
    run("codesign", "--verify", "--strict", str(args.app))
    print("Signed and verified with persistent Verdigris identity:", identity)
else:
    print("Persistent local identity prepared:", identity)
