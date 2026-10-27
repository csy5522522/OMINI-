; Build with Inno Setup 6.7.3+ (ISCC.exe). Paths are relative to this script.
#ifndef AppVersion
  #error Build with scripts/build_installer.ps1 to supply the application version.
#endif
#ifndef ReleaseDir
  #define ReleaseDir "..\artifacts\releases\" + AppVersion + "\Omini工具箱"
#endif

[Setup]
AppId={code:GetApplicationId}
AppName=Omini工具箱
AppVersion={#AppVersion}
AppVerName=Omini工具箱 {#AppVersion}
AppPublisher=Omini
DefaultDirName={localappdata}\Programs\Omini工具箱
DefaultGroupName=Omini工具箱
DisableDirPage=no
DisableProgramGroupPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\artifacts\installers\{#AppVersion}
OutputBaseFilename=Omini工具箱-{#AppVersion}-Setup
SetupIconFile=..\assets\omini_icon.ico
UninstallDisplayIcon={app}\Omini工具箱.exe
UninstallDisplayName=Omini工具箱
WizardStyle=modern
ShowLanguageDialog=no
UsePreviousLanguage=no
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=1
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=Omini工具箱安装程序
VersionInfoProductName=Omini工具箱

[Languages]
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#ReleaseDir}\*"; DestDir: "{app}"; Excludes: "README.md,文件夹同步使用说明.txt"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "安装版使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

; Remove obsolete application runtimes on upgrade. User data is under APPDATA.
[InstallDelete]
Type: files; Name: "{app}\tools\syncthing*"
Type: files; Name: "{app}\文件夹同步使用说明.txt"
Type: filesandordirs; Name: "{app}\_internal\omini_api"
Type: files; Name: "{app}\_internal\omini_api*.py*"
Type: files; Name: "{app}\_internal\brightdata*.py*"
Type: files; Name: "{app}\tools\ffmpeg*"
Type: files; Name: "{app}\tools\ffprobe.exe"
Type: files; Name: "{app}\tools\ffplay.exe"
Type: files; Name: "{app}\_internal\PySide6\Qt6Web*.dll"
Type: files; Name: "{app}\_internal\PySide6\QtWeb*.pyd"
Type: files; Name: "{app}\_internal\PySide6\QtWebEngineProcess.exe"
Type: files; Name: "{app}\_internal\PySide6\Qt6Quick*.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6Qml*.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6Pdf.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6OpenGL.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6VirtualKeyboard.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6Positioning.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6PrintSupport.dll"
Type: files; Name: "{app}\_internal\PySide6\Qt6SerialPort.dll"
Type: files; Name: "{app}\_internal\PySide6\QtPositioning.pyd"
Type: files; Name: "{app}\_internal\PySide6\QtPrintSupport.pyd"
Type: files; Name: "{app}\_internal\PySide6\QtQuickWidgets.pyd"
Type: files; Name: "{app}\_internal\PySide6\plugins\imageformats\qpdf.dll"
Type: files; Name: "{app}\_internal\PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll"
Type: filesandordirs; Name: "{app}\_internal\PySide6\plugins\position"
Type: filesandordirs; Name: "{app}\_internal\PySide6\resources"
Type: filesandordirs; Name: "{app}\_internal\PySide6\translations\qtwebengine_locales"
Type: filesandordirs; Name: "{app}\_internal\PySide6\qml"
Type: filesandordirs; Name: "{app}\_internal\PySide6\plugins\qmltooling"

[Icons]
Name: "{group}\Omini工具箱"; Filename: "{app}\Omini工具箱.exe"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\安装版使用说明.txt"
Name: "{group}\卸载 Omini工具箱"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Omini工具箱"; Filename: "{app}\Omini工具箱.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Omini工具箱.exe"; Description: "运行 Omini工具箱"; Flags: nowait postinstall skipifsilent

[Code]
function GetApplicationId(Param: String): String;
begin
  { Keep installer verification separate from an existing user installation. }
  if ExpandConstant('{param:OMINIVERIFY|0}') = '1' then
    Result := '{7E2C2E2F-5237-43BE-B64A-AC0A193146B8}'
  else
    Result := '{87C65C31-3F6B-4D75-A698-ED691D22FA37}';
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := '此向导将把 Omini工具箱安装到您的电脑。' + #13#10#13#10 +
    '您可以选择安装位置和桌面快捷方式。安装后可从开始菜单打开程序。' + #13#10#13#10 +
    '包含下载中心、校园网、学校课表、超级备忘录和 AI 配置中心。' + #13#10#13#10 +
    '可在设置中检查 GitHub 更新并下载安装。' + #13#10#13#10 +
    '卸载会保留个人设置、备忘录和账号数据。';
end;
