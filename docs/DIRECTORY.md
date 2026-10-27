# 目录说明

| 目录 / 文件 | 用途 |
| --- | --- |
| `src/` | 桌面窗口、下载、校园网、课表、备忘录、账号同步和 AI 配置中心源码 |
| `web/` | 网页端静态资源 |
| `assets/` | 图标和界面资源 |
| `tests/` | 自动化测试及公开测试素材 |
| `scripts/` | 引擎准备、构建、安装验收和发布脚本 |
| `installer/` | Windows 安装器配置与使用说明 |
| `deploy/` | 云端 Docker、数据库和部署配置 |
| `docs/`、`releases/` | 项目文档与历次更新说明 |
| `tools/` | 下载与账号池所用的第三方引擎、许可；EXE 不提交 Git |
| `extras/` | 已退出主程序的独立旧工具源码，仅供单独维护，不进入安装包 |
| `artifacts/installers/版本/` | 本地安装包及 SHA-256，保留旧版本 |
| `artifacts/releases/版本/` | 当前解包运行目录 |
| `artifacts/verification/` | 本地测试报告、截图及诊断记录 |
| `local-data/` | 历史恢复数据，保留原密文与密钥，不提交 Git、不进入安装包 |
| `.cache/` | 编译器、构建中间文件及可重新生成的缓存 |
| `app.py`、`web_server.py` | 稳定启动入口，加载 `src/` 中的实现 |
| `app_version.py` | 版本号唯一来源 |

在仓库根目录运行 `py -3.11 app.py`、`py -3.11 web_server.py --open` 或 `py -3.11 -m unittest discover -s tests -t .`。开发时可将 `src/` 加入编辑器的 Python 搜索路径；正式入口和测试入口已自动配置。

`./scripts/build_release.ps1` 默认输出到 `artifacts/releases/当前版本/`，编译缓存放入 `.cache/build/当前版本/`；`./scripts/build_installer.ps1` 输出到 `artifacts/installers/当前版本/`。本地产物与恢复数据均由 `.gitignore` 排除。用户运行数据仍保存在 `%APPDATA%/Omini工具箱/`。
