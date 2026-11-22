; NetScope – Windows installer (Inno Setup 6)
; Build: run Build-Installer.bat in the project folder (it builds NetScope.exe first).
;
; Npcap is NOT bundled: its free license does not allow redistribution. When the user ticks
; the option, setup runs Install-Npcap.bat, which downloads the official installer from
; npcap.com, verifies its digital signature and starts it.

#define AppName "NetScope"
#define AppVersion "1.0"
#define AppExe "NetScope.exe"

[Setup]
AppId={{6F2B1C3E-8D4A-4E5B-9C7F-NETSCOPE0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=NetScope
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=NetScope-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "npcap"; Description: "Download and install Npcap (needed for ""This PC"" website monitoring)"; GroupDescription: "Optional:"; Check: not NpcapInstalled

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Install-Npcap.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Install Npcap"; Filename: "{app}\Install-Npcap.bat"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\Install-Npcap.bat"; Description: "Install Npcap"; Tasks: npcap; Flags: shellexec waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function NpcapInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{sys}\Npcap\wpcap.dll'));
end;
