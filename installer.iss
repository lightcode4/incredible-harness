; Windows installer for Incredible Herness (built by Inno Setup in GitHub Actions).
[Setup]
AppName=Incredible Herness
AppVersion=0.1.0
AppPublisher=Incredible Herness
DefaultDirName={autopf}\Incredible Herness
DefaultGroupName=Incredible Herness
OutputDir=dist
OutputBaseFilename=Incredible-Herness-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\Incredible Herness.exe

[Files]
Source: "dist\Incredible Herness\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Incredible Herness"; Filename: "{app}\Incredible Herness.exe"
Name: "{autodesktop}\Incredible Herness"; Filename: "{app}\Incredible Herness.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Incredible Herness.exe"; Description: "Launch Incredible Herness"; Flags: nowait postinstall
