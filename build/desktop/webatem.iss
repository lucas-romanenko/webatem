; Inno Setup script — the Windows installer.
;
; Why an installer at all: the download used to be one self-extracting
; executable, which Windows' heuristic scanners block outright (0.6.3), and
; then a zip, which is not how a finished application arrives. This produces
; what every other Windows program produces: an installer that puts the app
; in the user's own Programs folder, adds a Start menu entry, and registers
; an uninstaller in Settings > Installed apps.
;
; PER-USER by design (PrivilegesRequired=lowest): no admin prompt, so it
; installs on a locked-down work machine, and Start-at-login (HKCU Run) and
; the data folder are already per-user.
;
; It is NOT code-signed — Windows will still say "Windows protected your PC"
; the first time (More info -> Run anyway). Signing is the only thing that
; removes that, and it costs money.
;
; Built by .github/workflows/desktop.yml:
;   iscc build/desktop/webatem.iss
;
; NO command-line flags, deliberately: the workflow runs under Git Bash,
; which rewrites "/Oout" into a Windows path (ISCC then reports two script
; filenames) and passes "//Oout" through literally (ISCC: unknown option).
; So the version arrives as a generated version.iss beside this file and the
; output directory is set below. The only argument is a relative path, which
; Git Bash leaves alone.
;
; Paths are relative to SourceDir, which is the repo root — no
; {#SourcePath}, whose trailing-backslash behaviour differs between versions.

#define MyAppName "WebATEM"
#define MyAppPublisher "Lucas Romanenko"
#define MyAppExeName "webatem.exe"
#define MyAppURL "https://github.com/lucas-romanenko/webatem"
; version.iss is written by the build (a single #define); a bare checkout
; compiles without it.
#ifexist "version.iss"
  #include "version.iss"
#endif
#ifndef MyVersion
  #define MyVersion "0.0.0"
#endif

[Setup]
; A fixed AppId is what makes the next version replace this one instead of
; installing beside it. Never change it.
AppId={{8F3C6B21-4F0E-4C5E-9A9C-6C1E0E6A51D7}
AppName={#MyAppName}
AppVersion={#MyVersion}
AppVerName={#MyAppName} {#MyVersion}
AppPublisher={#MyAppPublisher}
AppCopyright=Lucas Romanenko — MIT licensed
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyVersion}
SourceDir=..\..
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
; "x64" (not "x64compatible"): the newer spelling is an error on Inno 6.2,
; which some runner images still carry, while "x64" is accepted by every 6.x.
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=out
OutputBaseFilename=webatem-windows-x64-setup
SetupIconFile=build\desktop\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; An upgrade while it is running: close it first, restart it after.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The PyInstaller onedir build: webatem.exe beside its _internal folder.
Source: "dist\WebATEM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; collectstatic writes here at every start; the database, the settings and
; the log are the user's and are deliberately left behind (the same rule as
; every other application: uninstalling is not "throw away my setup").
Type: filesandordirs; Name: "{localappdata}\{#MyAppName}\staticfiles"
