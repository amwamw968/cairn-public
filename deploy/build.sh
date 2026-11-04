#!/usr/bin/env bash
# Cairn 镜像构建入口。版本号从 pyproject.toml 读，不写死。
#
# 用法：
#   ./build.sh                          多架构构建并推送（默认 linux/amd64 + linux/arm64）
#   ./build.sh --platform linux/amd64   只推指定架构（逗号分隔）
#   ./build.sh --local                  只在本机构建 linux/amd64 并 load 到本地
#
# The release Dockerfile includes the offline Python wheels and OCR system libraries.
# Each architecture is built and pushed separately before combining its manifest.
# System packages still require access to the configured Debian mirror.
#
# 环境变量：
#   CAIRN_IMAGE     推送镜像名（多架构推送时必填）
#   CAIRN_PLATFORMS 平台列表（默认 linux/amd64,linux/arm64）
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' ../pyproject.toml | head -1)
if [[ -z "$VERSION" ]]; then
  echo "读不到 pyproject.toml 的 version" >&2
  exit 2
fi

IMAGE="${CAIRN_IMAGE:-}"
PLATFORMS="${CAIRN_PLATFORMS:-linux/amd64,linux/arm64}"
LOCAL=0

usage() {
  cat <<'EOF'
用法：
  ./build.sh                          多架构构建并推送（默认 linux/amd64 + linux/arm64）
  ./build.sh --platform linux/amd64   只推指定架构（逗号分隔）
  ./build.sh --local                  只在本机构建 linux/amd64 并 load 到本地
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)
      LOCAL=1
      shift
      ;;
    --platform)
      PLATFORMS="${2:-}"
      [[ -n "$PLATFORMS" ]] || { echo "--platform 需要一个值" >&2; exit 2; }
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$LOCAL" == "1" ]]; then
  # Build the amd64 image locally, then start the existing compose services.
  echo "== 本地模式：只构建 linux/amd64 并 load 到本地（版本 ${VERSION}）"
  docker buildx build --platform linux/amd64 -f Dockerfile.release -t cairn:local --load ..
  docker compose up -d
  echo "== 完成：cairn:local（linux/amd64）"
  exit 0
fi

[[ -n "$IMAGE" ]] || { echo "Set CAIRN_IMAGE to a registry image before pushing" >&2; exit 2; }
echo "== 多架构构建并推送 ${IMAGE}:${VERSION}（${PLATFORMS}）"
IFS=',' read -r -a arches <<<"$PLATFORMS"
refs=()
for platform in "${arches[@]}"; do
  arch="${platform#linux/}"
  tag="${IMAGE}:${VERSION}-${arch}"

  echo "== [${platform}] 发布镜像 -> ${tag}"
  docker buildx build --platform "$platform" -f Dockerfile.release -t "$tag" --push ..

  refs+=("$tag")
done

echo "== 合成多架构标签 ${IMAGE}:${VERSION}"
docker buildx imagetools create -t "${IMAGE}:${VERSION}" "${refs[@]}"
docker buildx imagetools inspect "${IMAGE}:${VERSION}"
