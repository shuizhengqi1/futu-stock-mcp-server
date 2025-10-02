# 发布脚本使用说明

本目录包含用于构建和发布 Futu Stock MCP Server 到 PyPI 的脚本。

## 脚本列表

### 1. `publish.fish` - Fish Shell 发布脚本
适用于使用 Fish Shell 的用户。

**特性：**
- 自动检测和创建虚拟环境
- 自动激活虚拟环境（Fish Shell 方式）
- 自动安装依赖和构建工具
- 交互式发布流程
- 自动创建和推送 Git tags
- 可选的构建文件清理

**使用方法：**
```fish
# 确保你在项目根目录
cd /path/to/futu-stock-mcp-server

# 运行发布脚本
./scripts/publish.fish
```

### 2. `publish.sh` - Bash 发布脚本
适用于使用 Bash/Zsh 等传统 shell 的用户。

**特性：**
- 与 Fish 版本功能相同
- 使用 Bash 语法和虚拟环境激活方式

**使用方法：**
```bash
# 确保你在项目根目录
cd /path/to/futu-stock-mcp-server

# 运行发布脚本
./scripts/publish.sh
```

## 发布流程

两个脚本都会执行以下步骤：

1. **环境检查**
   - 检查 `uv` 和 `python` 是否安装
   - 获取项目根目录路径

2. **虚拟环境管理**
   - 检查 `.venv` 目录是否存在
   - 如果不存在，自动创建虚拟环境
   - 激活虚拟环境

3. **依赖安装**
   - 安装项目依赖 (`uv pip install -e .`)
   - 安装构建工具 (`build`, `twine`)

4. **构建准备**
   - 清理旧的构建文件
   - 读取当前版本号
   - 构建包 (`python -m build`)
   - 检查包的完整性

5. **发布确认**
   - 显示构建的文件列表
   - 询问是否发布到 PyPI
   - 检查 PyPI 认证配置

6. **发布和标签**
   - 上传到 PyPI
   - 可选：创建 Git tag
   - 可选：推送 tag 到远程仓库

7. **清理**
   - 可选：清理构建文件

## 环境配置

### PyPI 认证

推荐使用以下方式之一配置 PyPI 认证：

#### 方式1：环境变量
```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-your-api-token-here
```

#### 方式2：配置文件 `~/.pypirc`
```ini
[distutils]
index-servers = pypi

[pypi]
username = __token__
password = pypi-your-api-token-here
```

### 必要工具

确保安装了以下工具：

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或者使用 pip
pip install uv
```

## 版本管理

发布前请确保：

1. 更新 `pyproject.toml` 中的版本号
2. 更新 `CHANGELOG.md` 记录变更
3. 提交所有更改到 Git

## 故障排除

### 常见问题

1. **虚拟环境激活失败**
   - 检查 `.venv` 目录权限
   - 手动删除 `.venv` 目录重新创建

2. **PyPI 上传失败**
   - 检查网络连接
   - 验证 PyPI 认证信息
   - 确保版本号未被使用

3. **构建失败**
   - 检查 `pyproject.toml` 语法
   - 确保所有依赖都已安装

### 调试模式

如需调试，可以手动执行各个步骤：

```bash
# 激活虚拟环境
source .venv/bin/activate

# 构建
python -m build

# 检查
python -m twine check dist/*

# 上传（测试）
python -m twine upload --repository testpypi dist/*
```

## 注意事项

- 发布到 PyPI 的版本号不能重复使用
- 建议先在 TestPyPI 上测试发布流程
- 确保在发布前运行所有测试
- 发布后的包无法删除，只能发布新版本
