#!/bin/bash

# Futu Stock MCP Server PyPI 发布脚本
set -e

echo "📦 开始发布到 PyPI..."

# 检查必要的工具
command -v uv >/dev/null 2>&1 || { echo "❌ uv 未安装"; exit 1; }

# 检查是否在虚拟环境中
if [[ "$VIRTUAL_ENV" == "" ]]; then
    echo "⚠️  请先激活虚拟环境"
    exit 1
fi

# 安装发布工具
echo "🔧 安装发布工具..."
uv pip install build twine

# 清理旧的构建文件
echo "🧹 清理旧的构建文件..."
rm -rf dist/ build/ *.egg-info/

# 构建包
echo "🔨 构建包..."
python -m build

# 检查包
echo "🔍 检查包..."
python -m twine check dist/*

# 询问是否发布到 PyPI
read -p "是否发布到 PyPI? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "🚀 发布到 PyPI..."
    python -m twine upload dist/*
    echo "✅ 发布成功！"
else
    echo "📦 包已构建完成，位于 dist/ 目录"
    echo "💡 要发布到 PyPI，请运行: python -m twine upload dist/*"
fi

echo "🎉 完成！"
