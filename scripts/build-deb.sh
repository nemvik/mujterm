#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$(PYTHONPATH="$project_root/src" python3 -c 'from mujterm import __version__; print(__version__)')
control_version=$(sed -n 's/^Version: //p' "$project_root/packaging/control")
if [ "$control_version" != "$version" ]; then
    echo "Version mismatch: source is $version, Debian control is $control_version" >&2
    exit 1
fi

build_commit=unknown
if command -v git >/dev/null 2>&1 && git -C "$project_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    build_commit=$(git -C "$project_root" rev-parse --short=12 HEAD)
    if [ -n "$(git -C "$project_root" status --porcelain --untracked-files=normal)" ]; then
        build_commit="$build_commit-dirty"
    fi
fi

build_parent=$(mktemp -d)
package_root="$build_parent/mujterm_${version}_all"
trap 'rm -rf "$build_parent"' EXIT INT TERM

install -d "$package_root/DEBIAN"
install -d "$package_root/usr/bin"
install -d "$package_root/usr/lib/mujterm/mujterm"
install -d "$package_root/usr/share/applications"
install -d "$package_root/usr/share/metainfo"
install -d "$package_root/usr/share/icons/hicolor/scalable/apps"

install -m 0644 "$project_root/packaging/control" "$package_root/DEBIAN/control"
cp -a "$project_root/src/mujterm/." "$package_root/usr/lib/mujterm/mujterm/"
sed -i \
    -e 's/^BUILD_KIND = .*/BUILD_KIND = "debian"/' \
    -e "s/^BUILD_COMMIT: str | None = .*/BUILD_COMMIT: str | None = \"$build_commit\"/" \
    "$package_root/usr/lib/mujterm/mujterm/build_info.py"
find "$package_root/usr/lib/mujterm" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$package_root/usr/lib/mujterm" -type d -exec chmod 0755 {} +
find "$package_root/usr/lib/mujterm" -type f -exec chmod 0644 {} +
install -m 0755 "$project_root/packaging/mujterm" "$package_root/usr/bin/mujterm"
install -m 0755 "$project_root/packaging/mujterm-agent-hook" "$package_root/usr/bin/mujterm-agent-hook"
install -m 0755 "$project_root/packaging/mujterm-shell" "$package_root/usr/bin/mujterm-shell"
install -m 0755 "$project_root/packaging/mujterm-shell-hook" "$package_root/usr/bin/mujterm-shell-hook"
install -m 0644 "$project_root/data/io.github.viktornemcok.MujTerm.desktop" "$package_root/usr/share/applications/"
install -m 0644 "$project_root/data/io.github.viktornemcok.MujTerm.metainfo.xml" "$package_root/usr/share/metainfo/"
install -m 0644 "$project_root/data/io.github.viktornemcok.MujTerm.svg" "$package_root/usr/share/icons/hicolor/scalable/apps/"

install -d "$project_root/dist"
dpkg-deb --root-owner-group --build "$package_root" "$project_root/dist/mujterm_${version}_all.deb"
