; Web Canvas installer (Inno Setup 6). Built by .github/workflows/windows-installer.yml:
;   ISCC /DAppVersion=1.2.0 /DSourceDir=<PyInstaller's WebCanvas folder> /DOutputDir=<out> /DIconFile=<.ico> web_canvas.iss
; A per-user install (no administrator rights): the program under %LOCALAPPDATA%\Programs, and its
; data under %LOCALAPPDATA%\WebCanvas, which uninstalling leaves in place (the user's books).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6A2B0E4C-3F1D-4C7A-9B5E-2D8F1A7C4E90}
AppName=Web Canvas
AppVersion={#AppVersion}
AppPublisher=Web Canvas
DefaultDirName={localappdata}\Programs\WebCanvas
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=WebCanvas-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\WebCanvas.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Web Canvas"; Filename: "{app}\WebCanvas.exe"
Name: "{autodesktop}\Web Canvas"; Filename: "{app}\WebCanvas.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\WebCanvas.exe"; Description: "{cm:LaunchProgram,Web Canvas}"; Flags: nowait postinstall skipifsilent
