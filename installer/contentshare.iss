; Inno Setup script for ContentShare.
; Build:  iscc /DAppVersion=0.1.0 installer\contentshare.iss      (after python packaging\build.py)
; Result: dist\ContentShare-Setup-<version>.exe
;
; Installs for the current user only (no administrator rights, no UAC prompt).
; User data in %APPDATA%\ContentShareAI (settings, database, logs) is never touched.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change this id: Windows uses it to recognise upgrades of the same program.
AppId={{9361359C-B4A3-4B94-ABAC-E96EBC88A6C7}
AppName=ContentShare AI
AppVersion={#AppVersion}
AppVerName=ContentShare AI {#AppVersion}
DefaultDirName={autopf}\ContentShareAI
DefaultGroupName=ContentShare AI
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ContentShare-Setup-{#AppVersion}
SetupIconFile=..\build\contentshare.ico
UninstallDisplayIcon={app}\ContentShare.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; We close a running copy ourselves (below): the program lives in the tray, so Windows'
; own "close the application" request would only hide its window.
CloseApplications=no
RestartApplications=no
UsePreviousAppDir=yes
VersionInfoVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[InstallDelete]
; An upgrade installs over the old version, which would leave files the new one no longer has
; (for example help pages that were renamed, shown as a second copy of the topic). Only program
; files are listed here; user data is in %APPDATA%\ContentShareAI.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\ContentShare\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; The AppUserModelID is the same id the program sets for itself, so pop-up notifications and
; the taskbar belong to one "ContentShare".
Name: "{autoprograms}\ContentShare AI"; Filename: "{app}\ContentShare.exe"; AppUserModelID: "ContentShareAI.App"
Name: "{autoprograms}\ContentShare AI user guide"; Filename: "{app}\USER_GUIDE.html"
Name: "{autodesktop}\ContentShare AI"; Filename: "{app}\ContentShare.exe"; Tasks: desktopicon; AppUserModelID: "ContentShareAI.App"

[Run]
Filename: "{app}\ContentShare.exe"; Description: "Start ContentShare AI"; Flags: nowait postinstall skipifsilent

[Code]
// Ask a running ContentShare to quit (it asks the user first if copies are running).
procedure StopRunningCopy();
var
  ResultCode: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\ContentShare.exe');
  if FileExists(Exe) then
  begin
    if Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
      Sleep(3000);  // give it time to finish and release its files
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningCopy();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Exe: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopRunningCopy();
    Exe := ExpandConstant('{app}\ContentShare.exe');
    // Remove the "start with Windows" entry so Windows is not left pointing at a missing file.
    Exec(Exe, '--remove-startup', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    MsgBox('ContentShare AI was removed.' + #13#10 + #13#10 +
      'Your settings and request history were kept in your AppData\ContentShareAI folder.' + #13#10 +
      'If you had connected Gmail, open Google Account > Security > Third-party access ' +
      'and remove ContentShare AI there as well (or use Settings > Gmail > Disconnect ' +
      'before uninstalling).', mbInformation, MB_OK);
  end;
end;
