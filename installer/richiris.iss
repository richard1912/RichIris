; RichIris NVR - Inno Setup Script
; Build the distribution first: build_release.bat
; Then compile: ISCC.exe /DMyAppVersion=0.0.1 installer\richiris.iss

#define MyAppName "RichIris NVR"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.1"
#endif
#define MyAppPublisher "RichIris"
#define MyAppURL "https://github.com/richard1912/RichIris"
#define MyAppExeName "richiris.exe"
#define MyAppGUIName "richiris.exe"

[Setup]
AppId={{B8E7F3A1-5C2D-4E6F-9A8B-1D3E5F7A9C2B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\RichIris
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=RichIris-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\app\{#MyAppGUIName}
SetupIconFile=setup_icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Backend (PyInstaller output)
Source: "..\dist\richiris\richiris.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\richiris\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; Flutter app
Source: "..\dist\richiris\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

; Dependency downloader (deleted after install)
Source: "download_deps.ps1"; DestDir: "{app}"; Flags: ignoreversion deleteafterinstall

; NSSM for service management (tiny, needed during install)
Source: "..\dist\richiris\nssm.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\RichIris"; Filename: "{app}\app\{#MyAppGUIName}"
Name: "{group}\Uninstall RichIris"; Filename: "{uninstallexe}"
Name: "{commondesktop}\RichIris"; Filename: "{app}\app\{#MyAppGUIName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
; Install and start the Windows service
Filename: "{app}\nssm.exe"; Parameters: "install RichIris ""{app}\{#MyAppExeName}"""; StatusMsg: "Installing RichIris service..."; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set RichIris AppDirectory ""{app}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set RichIris DisplayName ""RichIris NVR"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set RichIris Description ""RichIris Network Video Recorder"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set RichIris Start SERVICE_AUTO_START"; Flags: runhidden waituntilterminated
; Note: AppStdout/AppStderr paths are set dynamically in CurStepChanged(ssPostInstall)
Filename: "{app}\nssm.exe"; Parameters: "start RichIris"; StatusMsg: "Starting RichIris service..."; Flags: runhidden waituntilterminated
; Launch the app after install
Filename: "{app}\app\{#MyAppGUIName}"; Description: "Launch RichIris"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Stop and remove the service
Filename: "{app}\nssm.exe"; Parameters: "stop RichIris"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "remove RichIris confirm"; Flags: runhidden waituntilterminated

[Code]
var
  DataDirPage: TInputDirWizardPage;
  ResultCode: Integer;
  DownloadFailed: Boolean;

function ReadExistingDataDir(): String;
var
  BootstrapPath: String;
  PrevDir: String;
  Lines: TArrayOfString;
  I: Integer;
  Line: String;
  Value: String;
begin
  Result := '';
  // {app} isn't available yet during InitializeWizard — find previous install from registry
  PrevDir := '';
  RegQueryStringValue(HKLM,
    'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{B8E7F3A1-5C2D-4E6F-9A8B-1D3E5F7A9C2B}_is1',
    'InstallLocation', PrevDir);
  if PrevDir = '' then
    PrevDir := ExpandConstant('{autopf}\RichIris');
  BootstrapPath := PrevDir + '\bootstrap.yaml';
  if FileExists(BootstrapPath) then
  begin
    if LoadStringsFromFile(BootstrapPath, Lines) then
    begin
      for I := 0 to GetArrayLength(Lines) - 1 do
      begin
        Line := Trim(Lines[I]);
        if Pos('data_dir:', Line) = 1 then
        begin
          Value := Trim(Copy(Line, Length('data_dir:') + 1, Length(Line)));
          // Strip surrounding quotes
          if (Length(Value) >= 2) and (Value[1] = '"') then
            Value := Copy(Value, 2, Length(Value) - 2);
          // Convert forward slashes back to backslashes for display
          StringChangeEx(Value, '/', '\', True);
          Result := Value;
          Break;
        end;
      end;
    end;
  end;
end;

procedure InitializeWizard();
var
  ExistingDir: String;
begin
  // Add a custom page after the install directory page for data storage location
  DataDirPage := CreateInputDirPage(wpSelectDir,
    'Data Storage Location',
    'Where should RichIris store its data files?',
    'Select the folder where RichIris will store recordings, database, and logs.'#13#10#13#10 +
    'This can be on a different drive from the application. Recordings can use ' +
    'hundreds of gigabytes, so choose a drive with enough free space.'#13#10#13#10 +
    'You can change the data location later from the app settings.',
    False, '');
  DataDirPage.Add('');

  // On upgrade: pre-populate from existing bootstrap.yaml
  ExistingDir := ReadExistingDataDir();
  if ExistingDir <> '' then
    DataDirPage.Values[0] := ExistingDir
  else
    DataDirPage.Values[0] := ExpandConstant('{commonappdata}\RichIris');
end;

function GetDataDir(Param: String): String;
begin
  Result := DataDirPage.Values[0];
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
  BootstrapPath: String;
  BootstrapContent: String;
  YamlDir: String;
  AppDir: String;
  PSScript: String;
begin
  if CurStep = ssInstall then
  begin
    // Stop existing service before upgrading
    if FileExists(ExpandConstant('{app}\nssm.exe')) then
    begin
      Exec(ExpandConstant('{app}\nssm.exe'), 'stop RichIris', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;

  if CurStep = ssPostInstall then
  begin
    AppDir := ExpandConstant('{app}');
    DataDir := DataDirPage.Values[0];

    // Create data subdirectories
    ForceDirectories(DataDir + '\database');
    ForceDirectories(DataDir + '\logs');
    ForceDirectories(DataDir + '\recordings');
    ForceDirectories(DataDir + '\thumbnails');
    ForceDirectories(DataDir + '\playback');

    // Always write bootstrap.yaml with user's chosen data dir
    BootstrapPath := AppDir + '\bootstrap.yaml';
    YamlDir := DataDir;
    StringChangeEx(YamlDir, '\', '/', True);
    BootstrapContent := 'data_dir: "' + YamlDir + '"' + #13#10 + 'port: 8700' + #13#10;
    SaveStringToFile(BootstrapPath, BootstrapContent, False);

    // Set NSSM log paths to the chosen data directory
    Exec(AppDir + '\nssm.exe',
      'set RichIris AppStdout "' + DataDir + '\logs\service-stdout.log"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(AppDir + '\nssm.exe',
      'set RichIris AppStderr "' + DataDir + '\logs\service-stderr.log"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Download dependencies if not already present
    PSScript := AppDir + '\download_deps.ps1';
    if FileExists(PSScript) then
    begin
      if not FileExists(AppDir + '\dependencies\ffmpeg.exe') or
         not FileExists(AppDir + '\dependencies\go2rtc\go2rtc.exe') then
      begin
        DownloadFailed := False;
        if not Exec('powershell.exe',
          '-ExecutionPolicy Bypass -File "' + PSScript + '" -InstallDir "' + AppDir + '"',
          '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
        begin
          DownloadFailed := True;
        end
        else if ResultCode <> 0 then
        begin
          DownloadFailed := True;
        end;

        if DownloadFailed then
        begin
          MsgBox('Some dependencies failed to download. The NVR may not work correctly until they are installed.' + #13#10 + #13#10 +
            'You can re-run the installer to retry, or download them manually into:' + #13#10 +
            AppDir + '\dependencies\', mbError, MB_OK);
        end;
      end;
    end;
  end;
end;
