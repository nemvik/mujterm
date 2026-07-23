#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_parent=$(mktemp -d)
package_root="$build_parent/mujterm_0.1.0_all"
trap 'rm -rf "$build_parent"' EXIT INT TERM

install -d "$package_root/DEBIAN"
install -d "$package_root/usr/bin"
install -d "$package_root/usr/lib/mujterm/mujterm"
install -d "$package_root/usr/share/applications"
install -d "$package_root/usr/share/metainfo"
install -d "$package_root/usr/share/icons/hicolor/scalable/apps"

install -m 0644 "$project_root/packaging/control" "$package_root/DEBIAN/control"
cp -a "$project_root/src/mujterm/." "$package_root/usr/lib/mujterm/mujterm/"
find "$package_root/usr/lib/mujterm" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$package_root/usr/lib/mujterm" -type d -exec chmod 0755 {} +
find "$package_root/usr/lib/mujterm" -type f -exec chmod 0644 {} +
install -m 0755 "$project_root/packaging/mujterm" "$package_root/usr/bin/mujterm"
install -m 0755 "$project_root/packaging/mujterm-agent-hook" "$package_root/usr/bin/mujterm-agent-hook"
install -m 0644 "$project_root/data/io.github.viktornemcok.MujTerm.desktop" "$package_root/usr/share/applications/"
install -m 0644 "$project_root/data/io.github.viktornemcok.MujTerm.metainfo.xml" "$package_root/usr/share/metainfo/"
install -m 0644 "$project_root/data/io.github.viktornemcok.MujTerm.svg" "$package_root/usr/share/icons/hicolor/scalable/apps/"

install -d "$project_root/dist"
dpkg-deb --root-owner-group --build "$package_root" "$project_root/dist/mujterm_0.1.0_all.deb"
