from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HELPERS = ROOT / "tools/packaging/posix"


@unittest.skipUnless(sys.platform == "darwin", "requires the macOS Security framework")
class TestAppleSigningExport(unittest.TestCase):
    def test_exports_exact_certificate_from_custom_keychain(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            work = Path(directory)
            exporter = work / "export"
            keychain = work / "source.keychain-db"

            def run(
                *args: str, input: str | None = None
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    args, input=input, capture_output=True, text=True, check=True
                )

            run(
                "xcrun",
                "swiftc",
                str(HELPERS / "export_signing_identity.swift"),
                "-o",
                str(exporter),
            )
            run("security", "create-keychain", "-p", "test-only", str(keychain))
            try:
                run("security", "unlock-keychain", "-p", "test-only", str(keychain))
                for index, name in enumerate(
                    ("Renewed Identity", "Renewed Identity", "Unrelated Identity")
                ):
                    prefix = work / str(index)
                    run(
                        "openssl",
                        "req",
                        "-x509",
                        "-newkey",
                        "rsa:2048",
                        "-nodes",
                        "-subj",
                        f"/CN={name}",
                        "-days",
                        "1",
                        "-keyout",
                        f"{prefix}.key",
                        "-out",
                        f"{prefix}.crt",
                    )
                    run(
                        "openssl",
                        "pkcs12",
                        "-export",
                        "-inkey",
                        f"{prefix}.key",
                        "-in",
                        f"{prefix}.crt",
                        "-name",
                        name,
                        "-passout",
                        "pass:test-only",
                        "-out",
                        f"{prefix}.p12",
                        "-keypbe",
                        "PBE-SHA1-3DES",
                        "-certpbe",
                        "PBE-SHA1-3DES",
                        "-macalg",
                        "sha1",
                    )
                    run(
                        "security",
                        "import",
                        f"{prefix}.p12",
                        "-k",
                        str(keychain),
                        "-P",
                        "test-only",
                        "-T",
                        str(exporter),
                        "-T",
                        "/usr/bin/security",
                    )
                fingerprint = (
                    run(
                        "openssl",
                        "x509",
                        "-in",
                        str(work / "1.crt"),
                        "-noout",
                        "-fingerprint",
                        "-sha1",
                    )
                    .stdout.strip()
                    .split("=", 1)[1]
                    .replace(":", "")
                )
                output = work / "selected.p12"
                run(
                    str(exporter),
                    fingerprint,
                    str(output),
                    str(keychain),
                    input="output-password",
                )
                exported = run(
                    "/usr/bin/openssl",
                    "pkcs12",
                    "-in",
                    str(output),
                    "-passin",
                    "pass:output-password",
                    "-nodes",
                ).stdout
                self.assertEqual(exported.count("-----BEGIN CERTIFICATE-----"), 1)
                self.assertEqual(exported.count("PRIVATE KEY-----"), 2)
                self.assertIn((work / "1.crt").read_text(), exported)
                self.assertNotIn((work / "0.crt").read_text(), exported)
                self.assertNotIn((work / "2.crt").read_text(), exported)
                imported = work / "runner.keychain-db"
                run("security", "create-keychain", "-p", "test-only", str(imported))
                try:
                    run(
                        "security",
                        "import",
                        str(output),
                        "-k",
                        str(imported),
                        "-P",
                        "output-password",
                        "-T",
                        "/usr/bin/codesign",
                    )
                finally:
                    run("security", "delete-keychain", str(imported))
                missing = subprocess.run(
                    [str(exporter), "0" * 40, str(work / "missing.p12"), str(keychain)],
                    input="output-password",
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn("No identity matches", missing.stderr)
                self.assertFalse((work / "missing.p12").exists())
            finally:
                run("security", "delete-keychain", str(keychain))


@unittest.skipUnless(
    os.name == "posix", "requires POSIX shell utilities and permissions"
)
class TestAppleSigningUpload(unittest.TestCase):
    def test_failed_upload_can_resume_same_pair_with_repo_argument(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            work = Path(directory)
            helper_dir = work / "tools/packaging/posix"
            helper_dir.mkdir(parents=True)
            script = helper_dir / "upload_apple_signing_secrets.sh"
            shutil.copyfile(HELPERS / script.name, script)
            binary_dir = work / "bin"
            binary_dir.mkdir()
            stubs = {
                "security": "#!/bin/sh\nprintf ' 1) %040d \"Developer ID Application: Test\"\\n' 1\n",
                "xcrun": '#!/bin/sh\nprintf test-p12 > "$4"\n',
                "gh": (
                    '#!/bin/sh\n[ "$4" = "--repo" ] && [ "$5" = "test/repository" ] || exit 90\n'
                    f'cat > "{work}/$3.upload"\n'
                    f'[ "$3" != APPLE_CERTIFICATE_PASSWORD ] || [ -f "{work}/retry" ]\n'
                ),
            }
            for name, source in stubs.items():
                executable = binary_dir / name
                executable.write_text(source)
                executable.chmod(0o700)
            env = dict(os.environ, PATH=f"{binary_dir}:{os.environ['PATH']}")
            result = subprocess.run(
                [
                    "sh",
                    str(script),
                    "--repo",
                    "test/repository",
                    "--keychain",
                    "custom chain",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Upload incomplete", result.stderr)
            recovery_dirs = list((work / "out").iterdir())
            self.assertEqual(len(recovery_dirs), 1)
            recovery = recovery_dirs[0]
            self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
            password = (recovery / "password").read_bytes()
            self.assertEqual(
                (work / "APPLE_CERTIFICATE_PASSWORD.upload").read_bytes(), password
            )
            (work / "retry").touch()
            subprocess.run(
                [
                    "sh",
                    str(script),
                    "--repo",
                    "test/repository",
                    "--resume",
                    str(recovery),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                (work / "APPLE_CERTIFICATE_PASSWORD.upload").read_bytes(), password
            )
            self.assertEqual(
                (work / "APPLE_CERTIFICATE_P12.upload").read_text().strip(),
                "dGVzdC1wMTI=",
            )
            self.assertFalse(recovery.exists())


if __name__ == "__main__":
    unittest.main()
