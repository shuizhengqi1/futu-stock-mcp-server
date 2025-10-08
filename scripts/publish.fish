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
set current_version (python3 -c "
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

# 检查版本是否已经发布到 PyPI
echo "🔍 检查版本是否已发布到 PyPI..."
set version_exists (python3 -c "
import urllib.request
import json
import sys

try:
    url = 'https://pypi.org/pypi/futu-stock-mcp-server/json'
    with urllib.request.urlopen(url) as response:
        data = json.loads(response.read().decode())
        releases = data.get('releases', {})
        if '$current_version' in releases:
            print('exists')
        else:
            print('not_exists')
except Exception as e:
    print('error')
    print(f'Error checking PyPI: {e}', file=sys.stderr)
")

if test "$version_exists" = "exists"
    echo "⚠️  版本 v$current_version 已经发布到 PyPI"
    echo "🔄 需要升级版本号..."

    # 自动升级版本号（增加补丁版本）
    set new_version (python3 -c "
import re

# 读取当前版本
with open('pyproject.toml', 'r') as f:
    content = f.read()

# 提取版本号
match = re.search(r'version\s*=\s*[\"\'](.*?)[\"\']', content)
if not match:
    print('Error: Could not find version in pyproject.toml')
    exit(1)

current = match.group(1)
parts = current.split('.')

# 增加补丁版本号
if len(parts) >= 3:
    parts[2] = str(int(parts[2]) + 1)
else:
    parts.append('1')

new_version = '.'.join(parts)
print(new_version)
")
    exit_on_error

    echo "📝 升级版本: v$current_version -> v$new_version"

    # 更新 pyproject.toml 中的版本号
    python3 -c "
import re

# 读取文件
with open('pyproject.toml', 'r') as f:
    content = f.read()

# 替换版本号
new_content = re.sub(
    r'(version\s*=\s*[\"\']).+?([\"\'])',
    r'\g<1>$new_version\g<2>',
    content
)

# 写回文件
with open('pyproject.toml', 'w') as f:
    f.write(new_content)

print('Version updated successfully')
"
    exit_on_error

    # 更新当前版本变量
    set current_version $new_version
    echo "✅ 版本已更新为: v$current_version"

else if test "$version_exists" = "error"
    echo "⚠️  无法检查 PyPI 版本，继续发布..."
else
    echo "✅ 版本 v$current_version 尚未发布，可以继续"
end

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

            # 检查是否有未提交的更改
            set git_status (git status --porcelain)
            if test -n "$git_status"
                echo "📝 检测到未提交的更改，准备提交代码..."

                # 显示更改的文件
                echo "📋 更改的文件:"
                git status --short

                read -P "是否提交这些更改? (Y/n): " -n 1 commit_confirm
                switch $commit_confirm
                    case n N
                        echo ""
                        echo "⏭️  跳过代码提交"
                    case '*'
                        echo ""
                        echo "📤 提交代码更改..."

                        # 添加所有更改的文件
                        git add .
                        exit_on_error

                        # 提交更改
                        git commit -m "chore: bump version to v$current_version and publish to PyPI"
                        exit_on_error

                        echo "✅ 代码已提交"

                        # 询问是否推送到远程仓库
                        read -P "是否推送代码到远程仓库? (Y/n): " -n 1 push_code_confirm
                        switch $push_code_confirm
                            case n N
                                echo "⏭️  跳过推送代码"
                            case '*'
                                echo "📤 推送代码到远程仓库..."
                                git push
                                if test $status -eq 0
                                    echo "✅ 代码已推送到远程仓库"
                                else
                                    echo "⚠️  代码推送失败，请手动推送"
                                end
                        end
                end
            else
                echo "✅ 没有未提交的更改"
            end

            # 创建 git tag
            read -P "是否创建 git tag v$current_version? (Y/n): " -n 1 tag_confirm
            switch $tag_confirm
                case n N
                    echo "⏭️  跳过创建 git tag"
                case '*'
                    echo ""
                    echo "🏷️  创建 git tag v$current_version..."
                    git tag -a "v$current_version" -m "Release v$current_version"
                    if test $status -eq 0
                        echo "✅ Git tag v$current_version 已创建"

                        read -P "是否推送 tag 到远程仓库? (Y/n): " -n 1 push_tag_confirm
                        switch $push_tag_confirm
                            case n N
                                echo "⏭️  跳过推送 tag"
                            case '*'
                                echo "📤 推送 tag 到远程仓库..."
                                git push origin "v$current_version"
                                if test $status -eq 0
                                    echo "✅ Tag 已推送到远程仓库"
                                else
                                    echo "⚠️  Tag 推送失败，请手动推送"
                                end
                        end
                    else
                        echo "⚠️  创建 git tag 失败"
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
