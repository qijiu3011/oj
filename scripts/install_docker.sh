#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Docker 安装脚本 (Ubuntu / CentOS)
# 在 OJ 服务器上以 root 或 sudo 执行:
#   chmod +x install_docker.sh && sudo ./install_docker.sh
# ---------------------------------------------------------------------------
set -euo pipefail

log() { printf "\033[32m[+] %s\033[0m\n" "$*"; }
err() { printf "\033[31m[-] %s\033[0m\n" "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    err "请用 root 或 sudo 执行此脚本"
    exit 1
fi

# --- 检测发行版 ---
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="$ID"
else
    err "无法检测 Linux 发行版"
    exit 1
fi

install_docker_ubuntu() {
    log "检测到 Ubuntu，开始安装 Docker……"
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
}

install_docker_centos() {
    log "检测到 CentOS / RHEL，开始安装 Docker……"
    yum install -y -q yum-utils
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    yum install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
}

case "$OS_ID" in
    ubuntu|debian)
        install_docker_ubuntu
        ;;
    centos|rhel|fedora)
        install_docker_centos
        ;;
    *)
        err "不支持的发行版: $OS_ID"
        err "请参考 https://docs.docker.com/engine/install/ 手动安装"
        exit 1
        ;;
esac

# --- 拉取评测所需的镜像 ---
log "安装完成，拉取评测镜像（约 800 MB，首次较慢）……"
IMAGES=(
    "python:3.10-slim"
    "gcc:latest"
    "openjdk:17-slim"
    "node:20-slim"
)
for img in "${IMAGES[@]}"; do
    log "拉取 $img ……"
    docker pull "$img"
done

# --- 非 root 用户免 sudo ---
if [ -n "${SUDO_USER:-}" ]; then
    log "将用户 $SUDO_USER 加入 docker 组（免 sudo）"
    usermod -aG docker "$SUDO_USER"
    log "注意: $SUDO_USER 需重新登录后生效"
fi

log "=============================="
log "Docker 安装完毕!"
log "运行 docker --version 验证"
log "=============================="
