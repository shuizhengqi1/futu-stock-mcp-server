#!/bin/bash

# Futu Stock MCP Server PyPI 发布脚本 (Bash)
# 自动切换虚拟环境，构建并发布到PyPI

set -e

echo "📦 开始发布 Futu Stock MCP Server 到 PyPI..."

# 检查必要的工具
command -v uv >/dev/null 2>&1 || { echo "❌ uv 未安装，请先安装 uv"; exit 1; }
command -v python >/dev/null 2>&1 || { echo "❌ Python 未安装"; exit 1; }

# 获取项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
echo "📁 项目目录: $PROJECT_ROOT"

# 切换到项目目录
cd "$PROJECT_ROOT"

# 检查虚拟环境
VENV_PATH="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV_PATH" ]; then
    echo "🔧 创建虚拟环境..."
    uv venv
fi

# 激活虚拟环境 (Bash 方式)
echo "🔄 激活虚拟环境..."
source "$VENV_PATH/bin/activate"

# 验证虚拟环境
if [[ "$VIRTUAL_ENV" == "" ]]; then
    echo "❌ 虚拟环境激活失败"
    exit 1
fi

echo "✅ 虚拟环境已激活: $VIRTUAL_ENV"

# 安装项目依赖
echo "📥 安装项目依赖..."
uv pip install -e .

# 安装发布工具
echo "🔧 安装发布工具..."
uv pip install build twine

# 清理旧的构建文件
echo "🧹 清理旧的构建文件..."
rm -rf dist/ build/ *.egg-info/
mkdir -p dist/

# 读取当前版本
CURRENT_VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")
echo "📋 当前版本: v$CURRENT_VERSION"

# 构建包
echo "🔨 构建包..."
python -m build

# 检查包
echo "🔍 检查包..."
python -m twine check dist/*

# 列出构建的文件
echo "📦 构建的文件:"
ls -la dist/

# 询问是否发布到 PyPI
echo ""
read -p "是否发布 v$CURRENT_VERSION 到 PyPI? (y/N): " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "🚀 发布到 PyPI..."

    # 检查是否有 PyPI token
    if [[ -z "$TWINE_PASSWORD" ]]; then
        echo "⚠️  建议设置 TWINE_PASSWORD 环境变量"
        echo "💡 或者在 ~/.pypirc 中配置认证信息"
    fi

    python -m twine upload dist/futu_stock_mcp_server-$CURRENT_VERSION*

    if [ $? -eq 0 ]; then
        echo "✅ 发布成功！"
        echo "🔗 查看包: https://pypi.org/project/futu-stock-mcp-server/$CURRENT_VERSION/"

        # 创建 git tag
        read -p "是否创建 git tag v$CURRENT_VERSION? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            git tag -a "v$CURRENT_VERSION" -m "Release v$CURRENT_VERSION"
            echo "🏷️  Git tag v$CURRENT_VERSION 已创建"

            read -p "是否推送 tag 到远程仓库? (y/N): " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                git push origin "v$CURRENT_VERSION"
                echo "📤 Tag 已推送到远程仓库"
            fi
        fi
    else
        echo "❌ 发布失败"
        exit 1
    fi
else
    echo "📦 包已构建完成，位于 dist/ 目录"
    echo "💡 要手动发布到 PyPI，请运行:"
    echo "   python -m twine upload dist/*"
fi

echo ""
echo "🎉 完成！"

# 可选：清理构建文件
read -p "是否清理构建文件? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "🧹 清理构建文件..."
    rm -rf build/ *.egg-info/
    echo "✅ 清理完成"
fi
