; Inno Setup script for the Rotary installer. After PyInstaller has built dist\Rotary:
;   iscc /DAppVersion=0.2.0 packaging\windows\rotary.iss
; Produces packaging\windows\Output\Rotary-Setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise upgrades of the same app.
AppId={{9670BDAA-E49D-4C35-8E25-6635BEB1E5A4}
AppName=Rotary
AppVersion={#AppVersion}
AppVerName=Rotary {#AppVersion}
AppPublisher=Rotary
; Install for the current user only, so no administrator rights are needed.
; {autopf} then means %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest
DefaultDirName={autopf}\Rotary
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Rotary-Setup-{#AppVersion}
SetupIconFile=..\rotary.ico
UninstallDisplayIcon={app}\Rotary.exe
UninstallDisplayName=Rotary
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[InstallDelete]
; Clear out the previous version's libraries so an upgrade never mixes old and new.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\Rotary\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Rotary"; Filename: "{app}\Rotary.exe"; Comment: "Convert files between formats"
Name: "{autodesktop}\Rotary"; Filename: "{app}\Rotary.exe"; Comment: "Convert files between formats"

[Run]
Filename: "{app}\Rotary.exe"; Description: "Open Rotary now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The log file and LibreOffice scratch profile.
Type: filesandordirs; Name: "{localappdata}\Rotary"
