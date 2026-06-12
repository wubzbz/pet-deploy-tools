# PET Deploy Tools for LoongArch64

用于在 LoongArch64 (LA64) 平台上部署 [Python Environment Tools (PET)](https://github.com/wubzbz/python-environment-tools) 的安装脚本和本地化资源。

> PET 是 Microsoft Python 团队开发的 Python 环境发现工具，本项目为其提供 LA64 平台的二进制构建和 VSCode 扩展的中文翻译。

## 内容

| 路径 | 说明 |
|------|------|
| `scripts/install-pet.py` | PET 安装/更新脚本（Python 3.6+，零外部依赖） |
| `resource/i18n/ms-python.python/` | VSCode `ms-python.python` 扩展的中文翻译 |
| `resource/i18n/ms-python.vscode-python-envs/` | VSCode `ms-python.vscode-python-envs` 扩展的中文翻译 |

## 用法

```bash
# 安装或更新 PET
python3 scripts/install-pet.py

# 仅检查（不执行任何修改）
python3 scripts/install-pet.py --dry-run
```

## PET 二进制

PET 二进制发布在 [python-environment-tools Releases](https://github.com/wubzbz/python-environment-tools/releases) 中。

## 许可

与原项目一致。参见 PET 仓库的 LICENSE 文件。
