#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  exec cargo test --locked
fi

original_default="$(security default-keychain -d user | sed -e 's/^ *"//' -e 's/"$//')"
original_search=()
while IFS= read -r keychain; do original_search+=("$keychain"); done < <(security list-keychains -d user | sed -e 's/^ *"//' -e 's/"$//')
directory="$(mktemp -d "${TMPDIR:-/tmp}/cga-relay-keychain.XXXXXX")"
export CGA_RELAY_TEST_KEYCHAIN="$directory/tests.keychain-db"
export CGA_RELAY_TEST_KEYCHAIN_PASSWORD
CGA_RELAY_TEST_KEYCHAIN_PASSWORD="$(openssl rand -hex 32)"
if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  echo "::add-mask::$CGA_RELAY_TEST_KEYCHAIN_PASSWORD"
fi
cleanup() {
  security default-keychain -d user -s "$original_default"
  security list-keychains -d user -s "${original_search[@]}"
  security delete-keychain "$CGA_RELAY_TEST_KEYCHAIN"
  rmdir "$directory"
}
security create-keychain -p "$CGA_RELAY_TEST_KEYCHAIN_PASSWORD" "$CGA_RELAY_TEST_KEYCHAIN"
trap cleanup EXIT
security set-keychain-settings -lut 21600 "$CGA_RELAY_TEST_KEYCHAIN"
security unlock-keychain -p "$CGA_RELAY_TEST_KEYCHAIN_PASSWORD" "$CGA_RELAY_TEST_KEYCHAIN"
security list-keychains -d user -s "$CGA_RELAY_TEST_KEYCHAIN"
security default-keychain -d user -s "$CGA_RELAY_TEST_KEYCHAIN"
cargo test --locked
