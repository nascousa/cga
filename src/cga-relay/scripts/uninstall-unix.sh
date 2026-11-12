#!/bin/sh
set -eu
fail() { printf '%s\n' "error: $*" >&2; exit 1; }
[ "$#" -eq 0 ] || fail "No options are accepted; user configuration and credentials are always retained"
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || fail "HOME must name an existing user directory"
case "$(uname -s)" in
    Darwin)
        agent=$HOME/Library/LaunchAgents/com.nascousa.cga-relay.plist
        if [ -f "$agent" ]; then
            if launchctl print "gui/$(id -u)/com.nascousa.cga-relay" >/dev/null 2>&1; then
                launchctl bootout "gui/$(id -u)" "$agent" || fail "Stop the CGA-Relay LaunchAgent before uninstalling"
            fi
            rm "$agent"
        fi
        if [ -L "$HOME/.local/bin/cga-relay" ] && [ "$(readlink "$HOME/.local/bin/cga-relay")" = "$HOME/.local/share/cga-relay/bin/cga-relay" ]; then
            rm "$HOME/.local/bin/cga-relay"
        fi
        echo "Startup entry removed. Stop Relay before removing ~/.local/share/cga-relay."
        ;;
    Linux)
        unit=$HOME/.config/systemd/user/cga-relay.service
        if [ -f "$unit" ]; then
            systemctl --user disable --now cga-relay.service || fail "Stop the CGA-Relay service before uninstalling"
            rm "$unit"
            systemctl --user daemon-reload
        fi
        rm -f "$HOME/.config/autostart/cga-relay.desktop" "$HOME/.local/share/applications/cga-relay.desktop"
        if [ -L "$HOME/.local/bin/cga-relay" ] && [ "$(readlink "$HOME/.local/bin/cga-relay")" = "$HOME/.local/share/cga-relay/bin/cga-relay" ]; then
            rm "$HOME/.local/bin/cga-relay"
        fi
        echo "Startup entries removed. Quit the tray before removing ~/.local/share/cga-relay."
        ;;
    *) fail "Only macOS and Linux are supported" ;;
esac
echo "Configuration, scan state and logs in ~/.cga are retained. Sign out before uninstalling to remove saved account credentials."
