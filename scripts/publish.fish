#!/usr/bin/env fish

# Futu Stock MCP Server PyPI 发布脚本 (Fish Shell)
# 自动切换虚拟环境，构建并发布到PyPI

# Fish shell 中的错误处理
function exit_on_error
    if test $status -ne 0
        echo "❌ 命令执行失败，退出"
        exit 1
    end
end

echo "📦 开始发布 Futu Stock MCP Server 到 PyPI..."

# 检查必要的工具
if not command -v uv >/dev/null 2>&1
    echo "❌ uv 未安装，请先安装 uv"
    exit 1
end

if not command -v python3 >/dev/null 2>&1
    echo "❌ Python 未安装"
    exit 1
end

# 获取项目根目录
set project_root (dirname (dirname (realpath (status --current-filename))))
echo "📁 项目目录: $project_root"

# 切换到项目目录
cd $project_root

# 检查虚拟环境
set venv_path "$project_root/.venv"
if not test -d $venv_path
    echo "🔧 创建虚拟环境..."
    uv venv
    exit_on_error
end

# 激活虚拟环境 (Fish shell 方式)
echo "🔄 激活虚拟环境..."
source $venv_path/bin/activate.fish

# 验证虚拟环境
if test -z "$VIRTUAL_ENV"
    echo "❌ 虚拟环境激活失败"
    exit 1
end

echo "✅ 虚拟环境已激活: $VIRTUAL_ENV"

# 安装项目依赖
echo "📥 安装项目依赖..."
uv pip install -e .
exit_on_error

# 安装发布工具
echo "🔧 安装发布工具..."
uv pip install build twine
exit_on_error

# 清理旧的构建文件
echo "🧹 清理旧的构建文件..."
if test -d dist
    rm -rf dist/
end
if test -d build
    rm -rf build/
end
# 清理 egg-info 目录（使用 find 命令避免通配符问题）
find . -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true
mkdir -p dist/

# 读取当前版本（兼容 Python 3.10 及以下版本）
set current_version (python -c "
import sys
if sys.version_info >= (3, 11):
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
else:
    # 对于 Python 3.10 及以下版本，使用简单的文本解析
    import re
    with open('pyproject.toml', 'r') as f:
        content = f.read()
    match = re.search(r'version\s*=\s*[\"\'](.*?)[\"\']', content)
    if match:
        print(match.group(1))
    else:
        print('unknown')
        exit(1)
    exit(0)
print(data['project']['version'])
")
exit_on_error

echo "📋 当前版本: v$current_version"

# 构建包
echo "🔨 构建包..."
python -m build
exit_on_error

# 检查包
echo "🔍 检查包..."
python -m twine check dist/*
exit_on_error

# 列出构建的文件
echo "📦 构建的文件:"
ls -la dist/

# 询问是否发布到 PyPI
echo ""
read -P "是否发布 v$current_version 到 PyPI? (y/N): " -n 1 confirm

switch $confirm
    case y Y
        echo ""
        echo "🚀 发布到 PyPI..."

        # 检查是否有 PyPI token
        if test -z "$TWINE_PASSWORD"
            echo "⚠️  建议设置 TWINE_PASSWORD 环境变量"
            echo "💡 或者在 ~/.pypirc 中配置认证信息"
        end

        # 使用通配符上传所有构建的文件
        python -m twine upload dist/*
        set upload_status $status

        if test $upload_status -eq 0
            echo "✅ 发布成功！"
            echo "🔗 查看包: https://pypi.org/project/futu-stock-mcp-server/$current_version/"

            # 创建 git tag
            read -P "是否创建 git tag v$current_version? (y/N): " -n 1 tag_confirm
            switch $tag_confirm
                case y Y
                    echo ""
                    git tag -a "v$current_version" -m "Release v$current_version"
                    echo "🏷️  Git tag v$current_version 已创建"

                    read -P "是否推送 tag 到远程仓库? (y/N): " -n 1 push_confirm
                    switch $push_confirm
                        case y Y
                            git push origin "v$current_version"
                            echo "📤 Tag 已推送到远程仓库"
                    end
            end
        else
            echo "❌ 发布失败"
            exit 1
        end

    case '*'
        echo ""
        echo "📦 包已构建完成，位于 dist/ 目录"
        echo "💡 要手动发布到 PyPI，请运行:"
        echo "   python -m twine upload dist/*"
end

echo ""
echo "🎉 完成！"

# 可选：清理构建文件
read -P "是否清理构建文件? (y/N): " -n 1 cleanup_confirm
switch $cleanup_confirm
    case y Y
        echo ""
        echo "🧹 清理构建文件..."
        if test -d build
            rm -rf build/
        end
        find . -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true
        echo "✅ 清理完成"
end
