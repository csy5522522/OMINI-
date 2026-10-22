# 版本管理与发布

`main` 保存开发源码，正式发布使用 `v主版本.次版本.修订版本` 标签。旧标签和已发布附件保持不变，每次发布递增版本；当前系列的小幅功能接入和修正递增末段，大规模功能迭代递增次版本，兼容性变化递增主版本。

程序版本唯一来源为根目录 `app_version.py`。安装脚本和目录打包脚本读取同一版本，防止程序版本、安装包名称与 Git 标签不一致。每次发布同步更新 README、CHANGELOG、安装说明及 `releases/v版本.md`。

## 检查并构建

在 Windows Python 3.11 环境准备 `requirements.txt` 与 `tools/` 的下载及同步引擎后执行：

```powershell
$version = py -3.11 app_version.py
py -3.11 -m unittest tests.test_omini_api tests.omini_api.test_omini_multisite tests.omini_api.test_omini_site_discovery tests.omini_api.test_local_proxy_paths tests.test_queue tests.test_icloud_notes tests.test_updater -v
py -3.11 -m unittest tests.test_ai_assistants tests.test_codex_quota tests.test_pool_scheduling tests.test_text_notes tests.test_local_ffmpeg tests.test_timetable tests.test_class_timetable -v
py -3.11 -m unittest tests.test_accounts tests.test_desktop_settings tests.test_web_server -v
node tests/test_web_frontend.cjs
py -3.11 -m unittest tests.test_account_sync tests.test_components -v
py -3.11 -m tests.account_sync_check
./scripts/build_omini_api.ps1 -DistDir "dist-sync-$version" -WorkDir "build-sync-$version"
& "./dist-sync-$version/Omini工具箱/Omini工具箱.exe" --self-test-omini-api
& "./dist-sync-$version/Omini工具箱/Omini工具箱.exe" --self-test-ai-assistants
& "./dist-sync-$version/Omini工具箱/Omini工具箱.exe" --self-test-settings
py -3.11 scripts/verify_compact_release.py "dist-sync-$version/Omini工具箱" --report verification/compact-release.json
./scripts/build_installer.ps1
```

安装包生成到 `dist-installer-版本/`，附带 SHA-256 文件。可用 `scripts/verify_installer.py` 在临时目录验证安装、启动、覆盖升级和卸载后的数据保留。已有正式安装时使用 `py -3.11 scripts/verify_installer.py "dist-installer-版本/Omini工具箱-版本-Setup.exe" --isolated-registration`：同一安装包通过 `/OMINIVERIFY=1` 使用独立测试 AppId，测试结束核对原安装注册项保持一致。默认模式在已有安装时停止执行。

1.1.8 的 AI 助手检查包含：停服显示本地成员、额度与操作记录重启恢复、恢复上次运行的账号池，以及手动停止后保持停止。安装器检查还会逐字节验证升级和卸载保留成员凭据、加密配置、状态与操作记录。所有检查使用隔离数据目录。

完整构建只使用 PySide6-Essentials 的原生 Widgets，显式排除 Qt WebEngine、QML 及 Chromium 资源。FFmpeg 由本机安装提供，所有打包入口共用 `release_packaging.copy_release_tools`，排除 FFmpeg、ffprobe、ffplay 及其随包元数据。媒体验证脚本支持 `--ffmpeg` 指定本机现有程序，运行环境也可通过 PATH 提供。安装器使用 `lzma2/ultra64`，压缩耗时比旧快速配置更长，分发文件更小。

1.1.9 构建包含 `web/` 静态资源和 `web_server` 入口。构建后验证 EXE 的 `--web` 模式：在隔离 `--data-dir` 下注册邮箱、登录、退出、重启恢复会话，以及功能开关。个人账号数据库和桌面加密会话仅保存在 AppData，发布包只分发代码及静态资源。

## 提交与发布

先检查 `git diff`，只提交源码、文档、资源和测试。个人账号、凭据、HAR、运行日志与打包目录由 `.gitignore` 排除。首次从新电脑获取源码：

```powershell
git clone https://github.com/csy5522522/OMINI-.git
```

正式发布示例：

```powershell
git add app_version.py app.py omini_api omini_api_page.py omini_api_runner.py omini_api_self_test.py Start-Omini-Desktop.cmd scripts tests installer README.md CHANGELOG.md .gitignore docs releases
git commit -m "Release 1.1.3: integrate Omini API into the desktop toolbox"
git tag -a v1.1.3 -m "Omini工具箱 1.1.3"
git push --atomic origin main v1.1.3
py -3.11 scripts/publish_release.py --publish
```

发布脚本从 Git Credential Manager 读取本机已有的 GitHub 凭据，仅在内存中用于请求。它核对工作区、源码提交、远端标签、安装包校验和，再创建草稿、上传安装包与校验文件，最后发布为最新正式版本。已有公开版本与内容不同的同名附件会保留，修复请使用新版本号。

用户可在 GitHub Release 下载指定版本；1.1.2 及后续桌面版也可通过「设置 → 软件更新」升级。回看源码使用 `git show v1.1.3`，复现旧构建请在独立目录检出对应标签。

1.2.0 发布前增加 `tests.test_memos` 及桌面账号切换验收，验证图片归属、离线动作逐个同步、冲突副本与回滚历史。生产镜像与 MySQL 使用独立 Docker Compose 项目，确认 `https://my.omini.cn` 的 HTTPS、登录和备忘录同步后再推送版本。部署说明和秘密文件约定见 `deploy/README.md`；数据库备份、会话、SSH 凭据和验收账号数据均排除出仓库。
