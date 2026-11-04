#!/usr/bin/env bash
# Build an offline wheelhouse for both supported Linux architectures.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
TAR=deploy/wheelhouse.tar.gz
stage=$(mktemp -d "${TMPDIR:-/tmp}/cairn-wheelhouse.XXXXXXXX")
mkdir -p "$stage/amd64" "$stage/arm64" "$stage/wheelhouse"

for arch in amd64 arm64; do
  if [[ "$arch" == amd64 ]]; then
    platforms=(manylinux2014_x86_64 manylinux_2_28_x86_64)
  else
    platforms=(manylinux2014_aarch64 manylinux_2_28_aarch64)
  fi
  args=()
  for platform in "${platforms[@]}"; do args+=(--platform "$platform"); done
  "$PY" -m pip download --only-binary=:all: "${args[@]}" \
    --python-version 3.12 --implementation cp --abi cp312 \
    -d "$stage/$arch" -r deploy/requirements.txt
  for wheel in "$stage/$arch/"*.whl; do
    target="$stage/wheelhouse/${wheel##*/}"
    if [[ -e "$target" ]]; then
      cmp -s "$wheel" "$target" || { echo "Conflicting wheel: ${wheel##*/}" >&2; exit 1; }
    else
      cp "$wheel" "$target"
    fi
  done
done

"$PY" - "$stage" <<'PY'
import sys
import tarfile
from pathlib import Path

stage = Path(sys.argv[1])
def normalize(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info

with tarfile.open(stage / "wheelhouse.tar.gz", "w:gz") as archive:
    archive.add(stage / "wheelhouse", arcname="wheelhouse", recursive=False, filter=normalize)
    for wheel in sorted((stage / "wheelhouse").glob("*.whl")):
        archive.add(wheel, arcname=f"wheelhouse/{wheel.name}", filter=normalize)
PY
if [[ -e "$TAR" ]]; then
  cp "$TAR" "$stage/previous-wheelhouse.tar.gz"
fi
archive=$(mktemp deploy/.wheelhouse.XXXXXXXX)
cp "$stage/wheelhouse.tar.gz" "$archive"
mv "$archive" "$TAR"
printf 'Wheelhouse: %s\nPrevious archive and staging files: %s\n' "$TAR" "$stage"
