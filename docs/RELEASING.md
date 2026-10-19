# 版本管理与发布

`main` 保存开发源码，正式发布使用 `v主版本.次版本.修订版本` 标签。旧标签和已发布附件保持不变，每次发布递增版本；当前系列的小幅功能接入和修正递增末段，大规模功能迭代递增次版本，兼容性变化递增主版本。

程序版本唯一来源为根目录 `app_version.py`。安装脚本和目录打包脚本读取同一版本，防止程序版本、安装包名称与 Git 标签不一致。每次发布同步更新 README、CHANGELOG、安装说明及 `releases/v版本.md`。

## 检查并构建

在 Windows Python 3.11 环境准备 `requirements.txt` 与 `tools/` 的下载及同步引擎后执行：

```powershell
$version = py -3.11 app_version.py
py -3.11 -m unittest tests.test_omini_api tests.omini_api.test_omini_multisite tests.omini_api.test_omini_site_discovery tests.omini_api.test_local_proxy_paths tests.test_queue tests.test_icloud_notes tests.test_updater -v
./scripts/build_omini_api.ps1 -DistDir "dist-sync-$version" -WorkDir "build-sync-$version"
& "./dist-sync-$version/Omini工具箱/Omini工具箱.exe" --self-test-omini-api
./scripts/build_installer.ps1
```

安装包生成到 `dist-installer-版本/`，附带 SHA-256 文件。可用 `scripts/verify_installer.py` 在临时目录验证安装、启动、覆盖升级和卸载后的数据保留；该验证在本机已有正式安装时会保留现有安装并停止执行。

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
