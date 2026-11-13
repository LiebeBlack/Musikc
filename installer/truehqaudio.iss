; ---------------------------------------------------------------------------
; True HQ Audio — Inno Setup 6 installer script
;
; Build:  ISCC.exe installer\truehqaudio.iss /DAppVersion=2.5.9
; Output: installer\Output\TrueHQAudio-2.5.9-x64-Setup.exe
;
; The installer is per-user by default (no UAC prompt), which is the correct
; model for an audio player: it needs no service, no driver and no admin rights.
; Users who prefer a machine-wide install can relaunch the setup and choose it.
; ---------------------------------------------------------------------------

#define AppName "True HQ Audio"
#define AppShortName "TrueHQAudio"
#define AppPublisher "True HQ Audio Labs"
#define AppURL "https://truehq.audio"
#define AppExeName "TrueHQAudio.exe"
#define SourceDir "..\dist\TrueHQAudio"
#define IconFile "..\resources\icons\truehq.ico"

#ifndef AppVersion
  #define AppVersion "2.5.9"
#endif

[Setup]
AppId={{7C1B4E52-9F3A-4D2B-8E64-1A9F5D0C7B31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/support
AppUpdatesURL={#AppURL}/downloads
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
OutputDir=Output
OutputBaseFilename=TrueHQAudio-{#AppVersion}-x64-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
LicenseFile=LICENSE.txt
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
AppMutex=Local\TrueHQAudio.SingleInstance.v1b
ChangesAssociations=yes
DisableWelcomePage=no
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "associate"; Description: "Associate audio files with {#AppName}"; GroupDescription: "File associations:"; Flags: unchecked
Name: "autostart"; Description: "Start {#AppName} when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "{#AppExeName}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE.txt"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "Bit-perfect reference player"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: autostart

[Registry]
Root: HKA; Subkey: "Software\Classes\{#AppShortName}.Audio"; ValueType: string; ValueName: ""; ValueData: "{#AppName} audio file"; Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\{#AppShortName}.Audio\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\{#AppShortName}.Audio\shell\open"; ValueType: string; ValueName: ""; ValueData: "Play with {#AppName}"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\{#AppShortName}.Audio\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.flac\OpenWithProgids"; ValueType: string; ValueName: "{#AppShortName}.Audio"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.wav\OpenWithProgids"; ValueType: string; ValueName: "{#AppShortName}.Audio"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.aif\OpenWithProgids"; ValueType: string; ValueName: "{#AppShortName}.Audio"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.aiff\OpenWithProgids"; ValueType: string; ValueName: "{#AppShortName}.Audio"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.dsf\OpenWithProgids"; ValueType: string; ValueName: "{#AppShortName}.Audio"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.dff\OpenWithProgids"; ValueType: string; ValueName: "{#AppShortName}.Audio"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\{#AppExeName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\{#AppPublisher}\{#AppName}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\*.log"

[Code]
function InitializeSetup(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  if (Version.Major < 10) then
  begin
    MsgBox('{#AppName} requires Windows 10 or Windows 11 (x64).', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { The engine writes its settings under %LOCALAPPDATA%\TrueHQAudio; create it }
    { eagerly so the first launch is instant and the log directory exists.       }
    ForceDirectories(ExpandConstant('{localappdata}\TrueHQAudio\logs'));
    ForceDirectories(ExpandConstant('{localappdata}\TrueHQAudio\config'));
    ForceDirectories(ExpandConstant('{localappdata}\TrueHQAudio\cache'));
    ForceDirectories(ExpandConstant('{localappdata}\TrueHQAudio\playlists'));
  end;
end;
