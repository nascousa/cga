#!/bin/sh
set -eu
umask 077

fail() { printf '%s\n' "error: $*" >&2; exit 1; }
usage() {
    echo "Usage: sh install-unix.sh [--mode desktop|headless|cli] [--project-root PATH] [--no-start]"
}
os=$(uname -s)
case "$os" in Darwin) mode=cli ;; Linux) mode=desktop ;; *) fail "Only macOS and Linux are supported" ;; esac
project_root=$PWD
start=yes
while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode|--project-root)
            [ "$#" -ge 2 ] || fail "Missing value for $1"
            case "$1" in --mode) mode=$2 ;; --project-root) project_root=$2 ;; esac
            shift 2 ;;
        --no-start) start=no; shift ;;
        --help) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
case "$mode" in desktop|headless|cli) ;; *) fail "Invalid mode: $mode" ;; esac
[ "$os:$mode" != Darwin:desktop ] || fail "macOS is CLI-only; use --mode cli or --mode headless"
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || fail "HOME must name an existing user directory"
[ -d "$project_root" ] || fail "Project root does not exist: $project_root"
project_root=$(cd "$project_root" && pwd -P)
cr=$(printf '\r')
case "$HOME$project_root" in *'
'*|*"$cr"*) fail "Paths cannot contain line breaks" ;; esac
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
[ -x "$source_dir/bin/cga-relay" ] || fail "Run this installer from an extracted CGA-Relay package"

base=$HOME/.cga
config=$base/relay.env
mkdir -p "$base/state" "$base/logs"
chmod 700 "$base" "$base/state" "$base/logs"
if [ "$os" = Darwin ]; then
    destination=$HOME/.local/share/cga-relay
    executable=$destination/bin/cga-relay
    mkdir -p "$destination/bin" "$HOME/Library/LaunchAgents" "$HOME/.local/bin"
    launch_agent=$HOME/Library/LaunchAgents/com.nascousa.cga-relay.plist
    if [ -f "$launch_agent" ] && launchctl print "gui/$(id -u)/com.nascousa.cga-relay" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$launch_agent" || fail "Stop the existing LaunchAgent before upgrading"
    fi
    install -m 755 "$source_dir/bin/cga-relay" "$executable.new"
    mv -f "$executable.new" "$executable"
    rm -f "$launch_agent"
    ln -sf "$executable" "$HOME/.local/bin/cga-relay"
else
    destination=$HOME/.local/share/cga-relay
    executable=$destination/bin/cga-relay
    if [ -f "$HOME/.config/systemd/user/cga-relay.service" ]; then
        systemctl --user disable --now cga-relay.service || fail "Stop the existing user service before upgrading"
        rm "$HOME/.config/systemd/user/cga-relay.service"
        systemctl --user daemon-reload
    fi
    rm -f "$HOME/.config/autostart/cga-relay.desktop" "$HOME/.local/share/applications/cga-relay.desktop"
    if [ "$mode" = desktop ]; then
        python=/usr/bin/python3
        command -v "$python" >/dev/null 2>&1 || fail "Install python3, GTK3 and Ayatana AppIndicator before desktop installation"
        "$python" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk
try:
 gi.require_version("AyatanaAppIndicator3", "0.1")
 from gi.repository import AyatanaAppIndicator3
except ValueError:
 gi.require_version("AppIndicator3", "0.1")
 from gi.repository import AppIndicator3' || fail "Install python3-gi, gir1.2-gtk-3.0 and gir1.2-ayatanaappindicator3-0.1"
    fi
    mkdir -p "$destination/bin" "$HOME/.local/bin" "$HOME/.config/autostart" "$HOME/.local/share/applications"
    install -m 755 "$source_dir/bin/cga-relay" "$executable.new"
    mv -f "$executable.new" "$executable"
    ln -sf "$executable" "$HOME/.local/bin/cga-relay"
fi

if [ ! -e "$config" ]; then
    cat > "$config" <<EOF
AGENT_ID=desktop-relay
API_BASE_URL=http://127.0.0.1:18001
CONTROL_API_BASE_URL=http://127.0.0.1:18001
API_KEY_ENV=CONTEXTGRAPH_MCP_TOKEN
ACCOUNT_EMAIL=
ACCOUNT_TOKEN_ENV=CGA_ACCOUNT_TOKEN
PROJECT_ID=
PROJECT_ROOT=$project_root
STATE_DIR=$base/state
LOG_DIR=$base/logs
INCLUDE_GLOBS=
EXCLUDE_GLOBS=.git/**,node_modules/**,.venv/**,__pycache__/**,dist/**,build/**,target/**,<agent-state>/**
MAX_FILE_BYTES=512000
EOF
fi
chmod 600 "$config"
[ "$mode" != cli ] || { printf 'Installed CLI: %s\nConfig: %s\n' "$executable" "$config"; exit 0; }
command_name=serve
[ "$mode" != desktop ] || command_name=tray

if [ "$os" = Darwin ]; then
    xml_escape() { printf '%s' "$1" | sed 's/\&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g'; }
    cat > "$launch_agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.nascousa.cga-relay</string>
<key>ProgramArguments</key><array><string>$(xml_escape "$executable")</string><string>$command_name</string><string>--config</string><string>$(xml_escape "$config")</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>StandardOutPath</key><string>$(xml_escape "$base/logs/service.log")</string>
<key>StandardErrorPath</key><string>$(xml_escape "$base/logs/service-error.log")</string>
</dict></plist>
EOF
    plutil -lint "$launch_agent"
    if [ "$start" = yes ]; then launchctl bootstrap "gui/$(id -u)" "$launch_agent"; fi
else
    desktop_quote() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/`/\\`/g; s/\$/\\$/g; s/%/%%/g'; }
    rm -f "$HOME/.config/autostart/cga-relay.desktop"
    if [ "$mode" = desktop ]; then
        cat > "$HOME/.local/share/applications/cga-relay.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CGA-Relay
Comment=Local CGA gateway and account settings
Exec="$(desktop_quote "$executable")" tray --config "$(desktop_quote "$config")"
Icon=network-server
Terminal=false
Categories=Development;
EOF
        cp "$HOME/.local/share/applications/cga-relay.desktop" "$HOME/.config/autostart/cga-relay.desktop"
        if [ "$start" = yes ]; then
            command -v gio >/dev/null 2>&1 || fail "gio is required to launch the desktop entry; launch CGA-Relay from your application menu"
            gio launch "$HOME/.local/share/applications/cga-relay.desktop"
        fi
    else
        command -v systemctl >/dev/null 2>&1 || fail "systemd user services are unavailable; run cga-relay serve --config \"$config\" manually"
        mkdir -p "$HOME/.config/systemd/user"
        unit_quote() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/%/%%/g; s/\$/$$/g'; }
        cat > "$HOME/.config/systemd/user/cga-relay.service" <<EOF
[Unit]
Description=CGA-Relay local gateway
After=network.target
[Service]
Type=simple
ExecStart="$(unit_quote "$executable")" serve --config "$(unit_quote "$config")"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        if [ "$start" = yes ]; then systemctl --user enable --now cga-relay.service; fi
    fi
fi
printf 'Installed %s mode.\nConfig: %s\nLogs: %s\n' "$mode" "$config" "$base/logs"
echo 'Open the loopback settings URL recorded in state/settings-url.txt to sign in. Existing configuration and history are retained.'
