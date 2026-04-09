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
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\app\{#MyAppGUIName}
SetupIconFile=setup_icon.ico
SetupLogging=yes

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

; Visual C++ Runtime DLLs (required on clean Windows installs)
Source: "..\installer\vcredist\*.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\installer\vcredist\*.dll"; DestDir: "{app}\app"; Flags: ignoreversion

[Icons]
Name: "{group}\RichIris"; Filename: "{app}\app\{#MyAppGUIName}"
Name: "{group}\Uninstall RichIris"; Filename: "{uninstallexe}"
Name: "{commondesktop}\RichIris"; Filename: "{app}\app\{#MyAppGUIName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
; Download dependencies (ffmpeg, go2rtc, YOLO model) — runs BEFORE service start
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\download_deps.ps1"" -InstallDir ""{app}"""; StatusMsg: "Downloading dependencies (this may take a few minutes)..."; Flags: waituntilterminated
; Remove any existing service first (handles upgrade from different install path)
Filename: "{app}\nssm.exe"; Parameters: "stop RichIris"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "remove RichIris confirm"; Flags: runhidden waituntilterminated
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
; Remove firewall rules
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""RichIris Backend"""; Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""RichIris go2rtc RTSP"""; Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""RichIris go2rtc API"""; Flags: runhidden waituntilterminated

[Code]
var
  DataDirPage: TInputDirWizardPage;
  ResultCode: Integer;

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
begin
  if CurStep = ssInstall then
  begin
    // Stop and remove existing service before upgrading (prevents auto-restart during install)
    if FileExists(ExpandConstant('{app}\nssm.exe')) then
    begin
      Exec(ExpandConstant('{app}\nssm.exe'), 'stop RichIris', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{app}\nssm.exe'), 'remove RichIris confirm', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
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

    // Add firewall rules for backend API and go2rtc RTSP
    Exec('netsh', 'advfirewall firewall delete rule name="RichIris Backend"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('netsh', 'advfirewall firewall add rule name="RichIris Backend" dir=in action=allow protocol=tcp localport=8700 profile=private,public',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('netsh', 'advfirewall firewall delete rule name="RichIris go2rtc RTSP"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('netsh', 'advfirewall firewall add rule name="RichIris go2rtc RTSP" dir=in action=allow protocol=tcp localport=8554 profile=private,public',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('netsh', 'advfirewall firewall delete rule name="RichIris go2rtc API"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('netsh', 'advfirewall firewall add rule name="RichIris go2rtc API" dir=in action=allow protocol=tcp localport=1984 profile=private,public',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Dependencies are now downloaded via [Run] section (before service start)
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
  DataDir: String;
  Lines: TArrayOfString;
  I: Integer;
  Line: String;
  Value: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    AppDir := ExpandConstant('{app}');

    // Read data_dir from bootstrap.yaml before deleting install dir
    DataDir := '';
    if FileExists(AppDir + '\bootstrap.yaml') then
    begin
      if LoadStringsFromFile(AppDir + '\bootstrap.yaml', Lines) then
      begin
        for I := 0 to GetArrayLength(Lines) - 1 do
        begin
          Line := Trim(Lines[I]);
          if Pos('data_dir:', Line) = 1 then
          begin
            Value := Trim(Copy(Line, Length('data_dir:') + 1, Length(Line)));
            if (Length(Value) >= 2) and (Value[1] = '"') then
              Value := Copy(Value, 2, Length(Value) - 2);
            StringChangeEx(Value, '/', '\', True);
            DataDir := Value;
            Break;
          end;
        end;
      end;
    end;

    // Delete install directory (contains app files, dependencies, configs)
    if DirExists(AppDir) then
      DelTree(AppDir, True, True, True);

    // Prompt to delete data directory (recordings, database, logs)
    if (DataDir <> '') and DirExists(DataDir) then
    begin
      if MsgBox('Do you want to delete all RichIris data files?' + #13#10 + #13#10 +
        'Location: ' + DataDir + #13#10 + #13#10 +
        'WARNING: This will permanently delete all recordings, database, thumbnails, and logs. This cannot be undone.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
