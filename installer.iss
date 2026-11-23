; Windows installer for LightCode⁴ (built by Inno Setup in GitHub Actions).
[Setup]
AppName=LightCode⁴
AppVersion=0.1.0
AppPublisher=LightCode⁴
DefaultDirName={autopf}\LightCode4
DefaultGroupName=LightCode⁴
OutputDir=dist
OutputBaseFilename=LightCode4-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\LightCode4.exe
SetupIconFile=assets\icon.ico

[Files]
Source: "dist\LightCode4\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\LightCode⁴"; Filename: "{app}\LightCode4.exe"
Name: "{autodesktop}\LightCode⁴"; Filename: "{app}\LightCode4.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\LightCode4.exe"; Description: "Launch LightCode⁴"; Flags: nowait postinstall
