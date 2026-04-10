; RichIris NVR - Client-Only Windows Installer
; Packages only the Flutter Windows app (no backend service, no go2rtc,
; no ffmpeg, no firewall rules, no data directory). For users on a second
; PC connecting to an existing RichIris backend on the LAN.
;
; Build the full distribution first: build_release.bat (produces dist\richiris\app\*)
; Then compile: ISCC.exe /DMyAppVersion=0.0.1 installer\richiris_client.iss

#define MyAppName "RichIris Client"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.1"
#endif
#define MyAppPublisher "RichIris"
#define MyAppURL "https://github.com/richard1912/RichIris"
#define MyAppExeName "richiris.exe"

[Setup]
; Distinct AppId from the full installer so both can coexist on one machine.
AppId={{D2F4A7B3-1E8C-4A9D-B0F5-7C6E3A8D1B42}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\RichIris Client
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=RichIris-Client-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=setup_icon.ico
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Flutter Windows app (from build_release.bat output)
Source: "..\dist\richiris\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Visual C++ Runtime DLLs (required on clean Windows installs)
Source: "..\installer\vcredist\*.dll"; DestDir: "{app}"; Flags: ignoreversion

; Install-flavor marker — tells the Flutter updater this is a client-only install
; so it fetches RichIris-Client-Setup.exe from GitHub instead of the full installer
Source: "client_only.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
; Launch the app after install (interactive install: checkbox; silent/update: always)
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent runasoriginaluser
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent runasoriginaluser
