; 创懿基金量化投资平台 —— Inno Setup 6 安装脚本
; 构建：dist\tq-iquant（Nuitka onedir 产物），先运行 main\build_package.py
; 编译："C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss

#define AppName "创懿基金量化投资平台"
#define AppVersion "1.0.0"
#define AppPublisher "创懿私募基金"
#define AppExe "tq-iquant.exe"
#define DistDir "..\dist\tq-iquant"

[Setup]
; AppId 固定不变：升级安装时沿用同一卸载项，不会出现两个「程序和功能」条目
AppId={{3B0F4A52-9C61-4C7A-8E1D-2F5B7A9C0D31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName=D:\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 安装到 D:\ 根目录需要管理员权限；程序日常运行不提权
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename={#AppName}-{#AppVersion}-安装程序
SetupIconFile=icon\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 升级安装时若程序在运行，提示关闭
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Dirs]
Name: "{app}\data"
Name: "{app}\logs"
Name: "{app}\bridge"

[Files]
; 主程序与运行期资源（data/、logs/ 内的用户数据不在本清单内，升级不覆盖）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 桥脚本：必须在 QMT 客户端 Python 3.6.8 / 掘金终端内明文运行，只作为独立文件随包附带
; 只随包 QMT(8790 股票账户) 与 掘金(7801) 两个生产桥；信用账户腿 iquant_bridge_live.py 保留在仓库但不部署
Source: "..\live\bridge\iquant_bridge.py"; DestDir: "{app}\bridge"; Flags: ignoreversion
Source: "..\live\bridge\goldminer_bridge.py"; DestDir: "{app}\bridge"; Flags: ignoreversion
Source: "bridge-deploy.txt"; DestDir: "{app}\bridge"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  RemoveData: Boolean;

function LocalAppDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

{ 升级安装：已有 data 目录时提示会保留（不删除、不覆盖） }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if DirExists(ExpandConstant('{app}\data')) then
    Log('检测到已有数据目录，升级安装将保留用户数据');
end;

{ 卸载时询问是否删除数据目录；默认保留 }
function InitializeUninstall(): Boolean;
var
  Msg: String;
begin
  Result := True;
  RemoveData := False;
  Msg := '是否同时删除数据目录（数据库、配置、日志、交割文件）？' + #13#10#13#10 +
         '选择「否」将保留数据，便于将来重新安装后继续使用。';
  if DirExists(ExpandConstant('{app}\data')) or DirExists(LocalAppDataDir()) then
  begin
    if MsgBox(Msg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      RemoveData := True;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveData then
  begin
    DelTree(ExpandConstant('{app}\data'), True, True, True);
    DelTree(ExpandConstant('{app}\logs'), True, True, True);
    DelTree(ExpandConstant('{app}\config.yaml'), True, True, True);
    { 安装目录不可写时程序会回落到 %LOCALAPPDATA%\{#AppName} 存数据 }
    DelTree(LocalAppDataDir(), True, True, True);
  end;
end;
