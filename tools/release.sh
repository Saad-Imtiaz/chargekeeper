#!/bin/bash
# Build the release archive for the current VERSION from its git tag, and test it unpacked.
#
#   tools/release.sh      writes dist/kernelthrottle-VERSION.tar.gz and .sha256
set -euo pipefail
cd "$(dirname "$0")/.."

version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' kernelthrottle/__init__.py)
tag="v$version"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null || { echo "No tag $tag: git tag $tag" >&2; exit 1; }
[ "$(git rev-parse "$tag^{commit}")" = "$(git rev-parse HEAD)" ] || { echo "$tag is not this commit" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Commit your changes first" >&2; exit 1; }
[ -f "docs/releases/$tag.md" ] || { echo "Missing docs/releases/$tag.md" >&2; exit 1; }
grep -q "^## $version " CHANGELOG.md || { echo "No \"## $version\" heading in CHANGELOG.md" >&2; exit 1; }

mkdir -p dist
name="kernelthrottle-$version"
git archive --format=tar.gz --prefix="$name/" -o "dist/$name.tar.gz" "$tag"
(cd dist && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")

check=$(mktemp -d)
trap 'rm -rf "$check"' EXIT
tar -xzf "dist/$name.tar.gz" -C "$check"
(cd "$check/$name" && python3 -m unittest discover tests 2>&1 | tail -1 && ./install.sh --dry-run --other > /dev/null)
echo "Built and tested:"
ls -l "dist/$name.tar.gz" "dist/$name.tar.gz.sha256"
