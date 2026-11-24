# 发布脚本使用说明

本目录包含用于发布 Futu Stock MCP Server 到 PyPI 的自动化脚本。

## 可用脚本

### 1. publish.sh (推荐)
Bash 版本的发布脚本，兼容性最好。

```bash
# 运行发布脚本
./scripts/publish.sh
```

### 2. publish.fish
Fish shell 版本的发布脚本。

```fish
# 运行发布脚本 (需要 Fish shell)
./scripts/publish.fish
```

## 使用前准备

### 1. 安装必要工具
```bash
# 安装 uv (Python 包管理器)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装 Python 3.10+
# macOS: brew install python
# Ubuntu: apt install python3
```

### 2. 配置 PyPI 认证

在 PyPI 的 **Account settings → API tokens** 创建项目范围的 token，Scope 选择 `futu-stock-mcp-server`。token 以 `pypi-` 开头。

#### 方法 1: 环境变量 (推荐)

Fish 中可隐藏输入，脚本会自动设置 Twine 用户名：

```fish
read --silent --export --prompt-str 'PyPI API token: ' TWINE_PASSWORD
./scripts/publish.fish
set --erase TWINE_PASSWORD
```

Bash 脚本使用下列环境变量：

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-your-api-token-here
```

#### 方法 2: 配置文件
创建 `~/.pypirc` 文件：
```ini
[distutils]
index-servers = pypi

[pypi]
username = __token__
password = pypi-your-api-token-here
```

## 发布流程

脚本会自动执行以下步骤：

1. **环境检查**: 验证 uv 和 Python 是否安装
2. **虚拟环境**: 创建/激活项目虚拟环境
3. **依赖安装**: 安装项目依赖和发布工具
4. **清理**: 删除旧的构建文件
5. **版本读取**: 从 pyproject.toml 读取当前版本
6. **构建**: 使用 `python -m build` 构建包
7. **检查**: 使用 `twine check` 验证包
8. **发布**: 询问确认后上传到 PyPI
9. **标签**: 可选创建 git tag 并推送

## 版本管理

在发布前，请确保：

1. 更新 `pyproject.toml` 中的版本号
2. 更新 `CHANGELOG.md` 记录变更
3. 提交所有更改到 git

## 故障排除

### 问题 1: uv 命令未找到
```bash
# 重新安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc  # 或 ~/.zshrc
```

### 问题 2: Python 版本不兼容
```bash
# 检查 Python 版本
python --version
# 需要 Python 3.10 或更高版本
```

### 问题 3: PyPI 认证失败

如果通过环境变量提供 token，可用 `test -n "$TWINE_PASSWORD"` 检查是否已设置。若使用 `~/.pypirc`，请在本机检查 `[pypi]` 的配置。若 PyPI 拒绝上传，请确认 token 有效且有权发布 `futu-stock-mcp-server`。

### 问题 4: 包已存在
如果版本号已经发布过，需要：
1. 更新 `pyproject.toml` 中的版本号
2. 重新运行发布脚本

## 手动发布

如果脚本失败，可以手动执行：

```bash
# 激活虚拟环境
source .venv/bin/activate

# 安装工具
pip install build twine

# 构建
python -m build

# 检查
python -m twine check dist/*

# 上传
python -m twine upload dist/*
```
