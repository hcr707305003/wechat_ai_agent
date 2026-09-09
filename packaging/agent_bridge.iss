#ifndef MyAppVersion
  #define MyAppVersion "0.1.1"
#endif

#define MyAppName "Agent Bridge"
#define MyAppExeName "AgentBridge.exe"

[Setup]
AppId={{6F609F61-F5C7-4B47-B80B-38E6FEA3D695}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Agent Bridge
DefaultDirName={autopf}\Agent Bridge
DefaultGroupName=Agent Bridge
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=AgentBridge-Setup-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter=AgentBridge.exe
UninstallDisplayIcon={app}\AgentBridge.exe
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=Agent Bridge Windows Installer
VersionInfoProductName={#MyAppName}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\AgentBridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Agent Bridge 管理面板"; Filename: "{app}\{#MyAppExeName}"; Parameters: "manager"; WorkingDir: "{app}"; AppUserModelID: "AgentBridge.Manager"
Name: "{autodesktop}\Agent Bridge"; Filename: "{app}\{#MyAppExeName}"; Parameters: "manager"; WorkingDir: "{app}"; Tasks: desktopicon; AppUserModelID: "AgentBridge.Manager"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "manager"; Description: "打开 Agent Bridge 管理面板"; Flags: nowait postinstall skipifsilent

[Code]
var
  DeleteUserDataCheckbox: TNewCheckBox;

procedure InitializeUninstallProgressForm();
begin
  DeleteUserDataCheckbox := TNewCheckBox.Create(UninstallProgressForm);
  DeleteUserDataCheckbox.Parent := UninstallProgressForm;
  DeleteUserDataCheckbox.Left := UninstallProgressForm.StatusLabel.Left;
  DeleteUserDataCheckbox.Top := UninstallProgressForm.StatusLabel.Top + ScaleY(48);
  DeleteUserDataCheckbox.Width := UninstallProgressForm.StatusLabel.Width;
  DeleteUserDataCheckbox.Caption := '同时删除配置、聊天历史、日志和缓存';
  DeleteUserDataCheckbox.Checked := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserDataDir: String;
begin
  if (CurUninstallStep = usPostUninstall) and DeleteUserDataCheckbox.Checked then
  begin
    UserDataDir := ExpandConstant('{localappdata}\AgentBridge');
    DelTree(UserDataDir, True, True, True);
  end;
end;
