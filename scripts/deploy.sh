#!/bin/bash

# Futu Stock MCP Server 部署脚本
set -e

echo "🚀 开始部署 Futu Stock MCP Server..."

# 检查必要的工具
command -v docker >/dev/null 2>&1 || { echo "❌ Docker 未安装"; exit 1; }
command -v docker-compose >/dev/null 2>&1 || { echo "❌ Docker Compose 未安装"; exit 1; }

# 检查环境文件
if [ ! -f .env ]; then
    echo "⚠️  .env 文件不存在，从示例文件复制..."
    cp .env.example .env
    echo "📝 请编辑 .env 文件配置您的参数"
    exit 1
fi

# 构建镜像
echo "🔨 构建 Docker 镜像..."
docker-compose build

# 启动服务
echo "🚀 启动服务..."
docker-compose up -d

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 10

# 健康检查
echo "🔍 检查服务状态..."
if curl -f http://localhost:8000/health >/dev/null 2>&1; then
    echo "✅ 服务启动成功！"
    echo "🌐 服务地址: http://localhost:8000"
    echo "📊 健康检查: http://localhost:8000/health"
else
    echo "❌ 服务启动失败，请检查日志:"
    docker-compose logs
    exit 1
fi

echo "🎉 部署完成！"
