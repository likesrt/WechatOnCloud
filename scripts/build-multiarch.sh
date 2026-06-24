#!/usr/bin/env bash
# 手动构建并推送双架构镜像到 GHCR（ghcr.io/likesrt）
#
# 分阶段设计：
#   1) 检查环境（docker buildx / GHCR 登录）
#   2) 并行构建 + 推送面板镜像 & 实例镜像（amd64 + arm64）
#   3) 验证镜像清单
#
# 用法：
#   # 交互式登录 → 构建 latest
#   ./scripts/build-multiarch.sh
#
#   # 指定版本标签（如 v1.2.0）
#   WOC_VERSION=v1.2.0 ./scripts/build-multiarch.sh
#
#   # 非交互式（环境变量 GITHUB_TOKEN 或 DOCKER_CONFIG 已配好 GHCR）
#   GHCR_USER=likesrt GHCR_TOKEN=ghp_xxx ./scripts/build-multiarch.sh
#
# 依赖：
#   - Docker >= 20.10（含 buildx 插件）
#   - 网络能连 ghcr.io（国内建议开代理，见脚本内 NOTES）
set -euo pipefail

# ─── 配置 ────────────────────────────────────────────────────────────────────
OWNER="likesrt"
TAG="${WOC_VERSION:-latest}"
PANEL_IMAGE="ghcr.io/${OWNER}/woc-panel:${TAG}"
WECHAT_IMAGE="ghcr.io/${OWNER}/wechat-on-cloud:${TAG}"
PLATFORMS="linux/amd64,linux/arm64"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="${WOC_VERSION:-dev-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo local)}"

# 颜色
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR]${NC}  $*" >&2; }

# ─── 阶段 0：前置检查 ────────────────────────────────────────────────────────
info "===== 阶段 0：环境检查 ====="

# Docker 可用
if ! command -v docker &>/dev/null; then
  err "Docker 未安装，请先安装 Docker。"
  exit 1
fi
ok "Docker $(docker --version | cut -d' ' -f3 | tr -d ',')"

# buildx 可用
if ! docker buildx version &>/dev/null; then
  err "docker buildx 不可用，请升级 Docker 或安装 buildx 插件。"
  exit 1
fi
ok "Buildx $(docker buildx version | cut -d' ' -f2)"

# 检查已有的 builder（优先复用，避免重复创建）
BUILDER="woc-multiarch"
if docker buildx inspect "$BUILDER" &>/dev/null; then
  ok "复用构建器 $BUILDER"
else
  info "创建构建器 $BUILDER（支持多架构）..."
  docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
  info "构建器准备就绪"
fi
docker buildx use "$BUILDER"

# GHCR 登录
if [ -n "${GHCR_TOKEN:-}" ]; then
  info "使用 GHCR_TOKEN 环境变量登录 ghcr.io..."
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-$OWNER}" --password-stdin
elif [ -n "${GITHUB_TOKEN:-}" ]; then
  info "使用 GITHUB_TOKEN 环境变量登录 ghcr.io..."
  echo "$GITHUB_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-$OWNER}" --password-stdin
else
  info "未检测到 GHCR_TOKEN/GITHUB_TOKEN，尝试交互式登录 ghcr.io..."
  echo "请先登录 GHCR（用户名 = GitHub 用户名，密码 = Personal Access Token，需 write:packages 权限）"
  docker login ghcr.io
fi

# 验证登录
if ! docker pull --platform linux/amd64 ghcr.io/likesrt/wechat-on-cloud:latest &>/dev/null 2>&1; then
  warn "GHCR 登录验证未通过或仓库为空，但不影响推送。"
fi
ok "GHCR 登录验证通过"

# ─── 阶段 1：双架构并行构建 ────────────────────────────────────────────────────
echo
info "===== 阶段 1：构建并推送镜像 ====="
info "  WOC_VERSION = ${VER}"
info "  面板镜像    = ${PANEL_IMAGE}"
info "  实例镜像    = ${WECHAT_IMAGE}"
info "  目标架构    = ${PLATFORMS}"

BUILD_START=$(date +%s)

# 面板镜像（后端 + 前端构建，需要传版本号）
echo
info ">>> [面板] 构建 + 推送中..."
docker buildx build \
  --provenance=false --sbom=false \
  --platform "$PLATFORMS" \
  --build-arg "WOC_VERSION=${VER}" \
  --cache-from type=gha,scope=panel \
  --cache-to type=gha,mode=max,scope=panel \
  -t "${PANEL_IMAGE}" \
  --push \
  "$ROOT/panel"
ok "面板镜像推送完成"

# 实例镜像（微信 + Chromium + Telegram 等基础环境）
info ">>> [实例] 构建 + 推送中..."
docker buildx build \
  --provenance=false --sbom=false \
  --platform "$PLATFORMS" \
  --cache-from type=gha,scope=wechat \
  --cache-to type=gha,mode=max,scope=wechat \
  -t "${WECHAT_IMAGE}" \
  --push \
  "$ROOT/docker"
ok "实例镜像推送完成"

BUILD_END=$(date +%s)
DURATION=$((BUILD_END - BUILD_START))

# ─── 阶段 2：验证 ────────────────────────────────────────────────────────────
echo
info "===== 阶段 2：验证镜像清单 ====="

verify_manifest() {
  local img="$1"
  local name="$2"
  if docker buildx imagetools inspect "$img" &>/dev/null; then
    ok "${name} 镜像清单确认存在："
    docker buildx imagetools inspect "$img" 2>/dev/null | head -10
  else
    err "${name} 镜像清单验证失败"
    return 1
  fi
}

verify_manifest "$PANEL_IMAGE" "面板"
verify_manifest "$WECHAT_IMAGE" "实例"

# ─── 完成 ────────────────────────────────────────────────────────────────────
echo
info "===== 完成 ====="
echo -e "  ${GREEN}面板镜像${NC}  ${PANEL_IMAGE}"
echo -e "  ${GREEN}实例镜像${NC}  ${WECHAT_IMAGE}"
echo -e "  ${CYAN}总耗时${NC}     ${DURATION} 秒"
echo
echo -e "下一步：部署时将 .env 的 WOC_IMAGE_PREFIX 设为 ghcr.io/likesrt，WOC_VERSION 设为 ${TAG}"
echo
info "提示：国内拉取 ghcr.io 可能较慢，可在部署端加反代前缀："
echo "  WOC_IMAGE_PREFIX=ghcr.nju.edu.cn/likesrt"
echo "  （GitHub Container Registry 南京大学镜像站）"
