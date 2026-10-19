; Build with Inno Setup 6.7.3+ (ISCC.exe). Paths are relative to this script.
#ifndef AppVersion
  #define AppVersion "1.1.1"
#endif
#ifndef ReleaseDir
  #define ReleaseDir "..\dist-sync-" + AppVersion + "\Omini工具箱"
#endif

[Setup]
AppId={{87C65C31-3F6B-4D75-A698-ED691D22FA37}
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
OutputDir=..\dist-installer-{#AppVersion}
OutputBaseFilename=Omini工具箱-{#AppVersion}-Setup
SetupIconFile=..\assets\omini_icon.ico
UninstallDisplayIcon={app}\Omini工具箱.exe
UninstallDisplayName=Omini工具箱
WizardStyle=modern
ShowLanguageDialog=no
Compression=lzma2/fast
SolidCompression=yes
LZMANumBlockThreads=2
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

[Icons]
Name: "{group}\Omini工具箱"; Filename: "{app}\Omini工具箱.exe"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\安装版使用说明.txt"
Name: "{group}\卸载 Omini工具箱"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Omini工具箱"; Filename: "{app}\Omini工具箱.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Omini工具箱.exe"; Description: "运行 Omini工具箱"; Flags: nowait postinstall skipifsilent

[Code]
procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := '此向导将把 Omini工具箱安装到您的电脑。' + #13#10#13#10 +
    '您可以选择安装位置和桌面快捷方式。安装后可从开始菜单打开程序。' + #13#10#13#10 +
    '文件夹同步支持 Tailscale、设备 ID 配对，以及手动、定时和自动同步。' + #13#10#13#10 +
    '卸载会保留个人设置和已同步文件。';
end;
