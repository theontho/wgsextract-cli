#!/bin/sh
# Upload this Mac's Developer ID signing identity to the repo's GitHub secrets,
# so the Distribution workflow can sign and notarize without this machine.
#
# Usage: upload_apple_signing_secrets.sh [--repo OWNER/NAME] [--keychain PATH]
#        upload_apple_signing_secrets.sh [--repo OWNER/NAME] --resume DIRECTORY
#
# Exports only the "Developer ID Application" identity (macOS asks you to allow
# the export), encrypts it as a .p12 with a fresh random password, and sets
# APPLE_CERTIFICATE_P12 / APPLE_CERTIFICATE_PASSWORD. Nothing secret is printed,
# and local files are removed after every secret is uploaded. Failed uploads
# keep the matching pair in a private directory so --resume can retry safely.
set -eu

repo=""
keychain=""
resume=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo|--keychain|--resume)
      [ "$#" -ge 2 ] && [ -n "$2" ] || { echo "$1 requires a value." >&2; exit 64; }
      case "$1" in
        --repo) repo="$2" ;;
        --keychain) keychain="$2" ;;
        --resume) resume="$2" ;;
      esac
      shift 2 ;;
    --help)
      echo "Usage: $0 [--repo OWNER/NAME] [--keychain PATH] [--resume DIRECTORY]"
      exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 64 ;;
  esac
done
command -v gh >/dev/null 2>&1 || { echo "gh (GitHub CLI) is required." >&2; exit 1; }
umask 077
if [ -n "$resume" ]; then
  [ -z "$keychain" ] || { echo "--resume cannot be combined with --keychain." >&2; exit 64; }
  work="$resume"
  for file in devid.p12 password identity; do
    [ -f "$work/$file" ] || { echo "Recovery directory is missing $file." >&2; exit 1; }
  done
else
  set --
  [ -z "$keychain" ] || set -- "$keychain"
  identities="$(security find-identity -v -p codesigning "$@" |
    sed -n 's/.* \([0-9A-Fa-f]\{40\}\) "\(Developer ID Application: [^"]*\)".*/\1 \2/p' | sort -u)"
  [ -n "$identities" ] || { echo "No valid Developer ID Application identity in the keychain." >&2; exit 1; }
  [ "$(printf '%s\n' "$identities" | wc -l)" -eq 1 ] || {
    printf 'Several Developer ID Application identities; select a keychain containing one:\n%s\n' "$identities" >&2
    exit 1
  }
  fingerprint="${identities%% *}"
  identity="${identities#* }"
  root="$(CDPATH='' cd -- "$(dirname "$0")/../../.." && pwd)"
  mkdir -p "$root/out"
  work="$(mktemp -d "$root/out/apple-signing.XXXXXX")"
  # Before upload starts, there is no remote state to recover.
  trap 'rm -f "$work/devid.p12" "$work/password" "$work/identity"; rmdir "$work"' EXIT
  openssl rand -hex 24 | tr -d '\n' > "$work/password"
  printf '%s' "$identity" > "$work/identity"
  echo "Exporting selected signing identity (allow the export when macOS asks)..."
  xcrun swift "$(dirname "$0")/export_signing_identity.swift" \
    "$fingerprint" "$work/devid.p12" "$@" < "$work/password"
  trap - EXIT
fi

set --
[ -z "$repo" ] || set -- --repo "$repo"
trap 'echo "Upload incomplete. Matching credentials retained in: $work" >&2; echo "Retry with --resume and the same --repo. Remove the directory after recovery." >&2' EXIT
base64 < "$work/devid.p12" > "$work/certificate.base64"
gh secret set APPLE_CERTIFICATE_P12 "$@" < "$work/certificate.base64"
gh secret set APPLE_CERTIFICATE_PASSWORD "$@" < "$work/password"
gh secret set APPLE_SIGNING_IDENTITY "$@" < "$work/identity"
echo "Uploaded \"$(cat "$work/identity")\" to GitHub secrets."
rm -f "$work/devid.p12" "$work/password" "$work/identity" "$work/certificate.base64"
rmdir "$work"
trap - EXIT
