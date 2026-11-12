#!/bin/sh
set -eu
umask 077
fail() { printf '%s\n' "error: $*" >&2; exit 1; }
kind=candidate
if [ "${1:-}" = --release ]; then kind=release; shift; fi
[ "$#" -eq 1 ] || fail "Usage: sh build-unix-package.sh [--release] NEW_OUTPUT_DIRECTORY"
case "$1" in /*) output=$1 ;; *) output=$PWD/$1 ;; esac
[ ! -e "$output" ] || fail "Output directory exists; released/candidate packages are never overwritten"
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
os=$(uname -s)
case "$os" in Darwin) platform=macos ;; Linux) platform=linux ;; *) fail "Build on macOS or Linux" ;; esac
case "$(uname -m)" in x86_64|amd64) arch=x64 ;; arm64|aarch64) arch=arm64 ;; *) fail "Unsupported architecture" ;; esac
version=$(tr -d '\r' < "$root/Cargo.toml" | sed -n 's/^version = "\([^"]*\)"$/\1/p')
[ -n "$version" ] || fail "Cannot read Cargo package version"
if [ "$kind" = release ]; then
    if [ "$os" = Darwin ]; then
        [ -n "${CGA_RELAY_APPLE_SIGNING_IDENTITY:-}" ] || fail "Apple Developer ID signing identity is required"
        [ -n "${CGA_RELAY_NOTARY_PROFILE:-}" ] || fail "A notarytool keychain profile is required"
    else
        [ -n "${CGA_RELAY_GPG_SIGNING_KEY:-}" ] || fail "A GPG signing key fingerprint is required"
    fi
fi
cd "$root"
cargo build --locked --release
target_dir=${CARGO_TARGET_DIR:-$root/target}
case "$target_dir" in /*) ;; *) target_dir=$root/$target_dir ;; esac
name=cga-relay-$version-$platform-$arch
[ "$kind" != candidate ] || name=$name-candidate
mkdir -p "$output/$name/bin" "$output/$name/scripts"
package=$output/$name
install -m 755 "$target_dir/release/cga-relay" "$package/bin/cga-relay"
install -m 644 scripts/install-unix.sh scripts/uninstall-unix.sh "$package/scripts/"
if [ "$os" = Darwin ]; then
    if [ "$kind" = release ]; then
        codesign --force --options runtime --timestamp --sign "$CGA_RELAY_APPLE_SIGNING_IDENTITY" "$package/bin/cga-relay"
    else
        codesign --force --sign - "$package/bin/cga-relay"
    fi
    codesign --verify --strict "$package/bin/cga-relay"
fi
cp "$root/../../LICENSE" "$package/LICENSE"
install_mode=desktop
[ "$os" != Darwin ] || install_mode=cli
cat > "$package/INSTALL.txt" <<EOF
CGA-Relay $version - $platform/$arch - $kind
Install: sh scripts/install-unix.sh --mode $install_mode
Headless: sh scripts/install-unix.sh --mode headless
CLI only: sh scripts/install-unix.sh --mode cli
Configuration and state: ~/.cga (retained on uninstall).
Requires Git for incremental indexing.
Linux desktop requires Python3, PyGObject, GTK3 and Ayatana AppIndicator.
Linux persistent account login requires secret-tool and a running Secret Service.
macOS is CLI-only, with optional headless LaunchAgent and login Keychain credentials.
No plaintext credential fallback.
Candidate packages are development builds, not signed/notarized formal releases.
EOF
archive=$output/$name.tar.gz
tar -czf "$archive" -C "$output" "$name"
if [ "$os" = Darwin ]; then
    ditto -c -k --keepParent "$package" "$output/$name.zip"
    if [ "$kind" = release ]; then
        set -- xcrun notarytool submit "$output/$name.zip" --keychain-profile "$CGA_RELAY_NOTARY_PROFILE" --wait --output-format plist
        [ -z "${CGA_RELAY_NOTARY_KEYCHAIN:-}" ] || set -- "$@" --keychain "$CGA_RELAY_NOTARY_KEYCHAIN"
        "$@" > "$output/notarization-result.plist"
        status=$(/usr/libexec/PlistBuddy -c 'Print :status' "$output/notarization-result.plist")
        [ "$status" = Accepted ] || fail "Apple notarization did not accept the CLI archive: $status"
        # Standalone Mach-O executables cannot be stapled; verify the online ticket.
        spctl --assess --type execute --verbose "$package/bin/cga-relay"
    fi
    shasum -a 256 "$archive" "$output/$name.zip" | sed "s|$output/||g" > "$output/$name.sha256"
else
    sha256sum "$archive" | sed "s|$output/||g" > "$output/$name.sha256"
    if [ "$kind" = release ]; then
        gpg --batch --yes --local-user "$CGA_RELAY_GPG_SIGNING_KEY" --armor --detach-sign "$archive"
        gpg --verify "$archive.asc" "$archive"
    fi
fi
printf 'Package: %s\n' "$archive"
