#define AppName "Clearview Analytics"
#define AppVersion "1.0.0"

[Setup]
AppId={{BBAA7CF6-7A5A-4D26-93CC-BB17A94FC28D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Clearview Analytics
DefaultDirName={localappdata}\Programs\Clearview Analytics
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer-output
OutputBaseFilename=ClearviewAnalyticsSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\ClearviewAnalytics.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist-desktop\ClearviewAnalytics\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\ClearviewAnalytics.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\ClearviewAnalytics.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClearviewAnalytics.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
