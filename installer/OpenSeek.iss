; OpenSeek for Windows: one installer that sets up the server (as a Windows service) and the desktop app.
; Build with installer\build_installer.py (it stages everything under installer\.build\stage first).
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

[Setup]
AppId={{6E1F3C52-8B0D-4D7A-9C41-2A5B7E90D4F3}
AppName=OpenSeek
AppVersion={#AppVersion}
AppVerName=OpenSeek {#AppVersion}
AppPublisher=OpenSeek authors
AppPublisherURL=https://github.com/obadevyh/openseek
AppSupportURL=https://github.com/obadevyh/openseek/issues
AppUpdatesURL=https://github.com/obadevyh/openseek/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\OpenSeek
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=.build\out
OutputBaseFilename=OpenSeekSetup-v{#AppVersion}-Windows-x64
SetupIconFile=..\desktop\icon.ico
UninstallDisplayIcon={app}\desktop\OpenSeek.exe
LicenseFile=.build\stage\installer-license.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: ".build\stage\server\*"; DestDir: "{app}\server"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: ".build\stage\desktop\*"; DestDir: "{app}\desktop"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: ".build\stage\service\*"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "support\openseek-service.ps1"; DestDir: "{app}\support"; Flags: ignoreversion
Source: ".build\stage\licenses\*"; DestDir: "{app}\licenses"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: ".build\stage\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: ".build\stage\README.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: ".build\stage\SOURCE-OFFER.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: ".build\stage\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\OpenSeek"; Filename: "{app}\desktop\OpenSeek.exe"; WorkingDir: "{app}\desktop"
Name: "{autoprograms}\Uninstall OpenSeek"; Filename: "{uninstallexe}"
Name: "{autodesktop}\OpenSeek"; Filename: "{app}\desktop\OpenSeek.exe"; WorkingDir: "{app}\desktop"; Tasks: desktopicon

[Registry]
; the desktop app's optional "Start with Windows" entry (written by the app, for the user who ticked it)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "OpenSeekDesktop"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\desktop\OpenSeek.exe"; Description: "Open OpenSeek now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
; the service definition (OpenSeekService.xml) is generated at install time, so the uninstaller does not know it
Type: filesandordirs; Name: "{app}\service"
Type: filesandordirs; Name: "{app}"

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\support\openseek-service.ps1"" -Action Uninstall -InstallDir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "OpenSeekServiceRemove"

[Code]
function PortArg(Name, Flag: String): String;
var v: String;
begin
  Result := '';
  v := ExpandConstant('{param:' + Name + '|0}');
  if (v <> '0') and (v <> '') then Result := ' ' + Flag + ' ' + v;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  rc: Integer;
  args: String;
begin
  if CurStep = ssPostInstall then begin
    WizardForm.StatusLabel.Caption := 'Setting up the OpenSeek service...';
    args := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\support\openseek-service.ps1') + '" -Action Install -InstallDir "' +
            ExpandConstant('{app}') + '" -User "' + ExpandConstant('{username}') + '" -Version "{#AppVersion}"' +
            PortArg('PORT', '-Port') + PortArg('ADMINPORT', '-AdminPort');
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), args, '', SW_HIDE, ewWaitUntilTerminated, rc) then rc := -1;
    Log('openseek-service.ps1 Install exit code ' + IntToStr(rc));
    if rc <> 0 then
      MsgBox('OpenSeek was installed, but its background service did not start (code ' + IntToStr(rc) + ').' + #13#10 +
             'You can start it from the OpenSeek dashboard, or look at the log in ' + ExpandConstant('{commonappdata}') + '\OpenSeek\data\logs.', mbError, MB_OK);
  end;
end;

{ Deleting the data is decided when the uninstaller actually runs (in its second process). Note: Check: functions of [UninstallRun] entries are
  evaluated at INSTALL time and frozen, so they cannot be used for this. }
function HasSwitch(const S: String): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), S) = 0 then begin
      Result := True;
      Exit;
    end;
end;

function WantDeleteData(): Boolean;
begin
  Result := HasSwitch('/DELETEDATA=1');
  if (not Result) and (not UninstallSilent) then
    Result := MsgBox('Also delete your OpenSeek data (cached thumbnails, paired devices and the server certificate)?' + #13#10#13#10 +
                     'Choose No to keep it, so a later reinstall picks up where you left off.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { the service has been stopped and removed by the [UninstallRun] step above by now }
  if CurUninstallStep = usPostUninstall then begin
    if WantDeleteData() then begin
      Log('Deleting ' + ExpandConstant('{commonappdata}\OpenSeek'));
      if not DelTree(ExpandConstant('{commonappdata}\OpenSeek'), True, True, True) then
        Log('Could not delete everything in ' + ExpandConstant('{commonappdata}\OpenSeek'));
    end else
      Log('Keeping your data in ' + ExpandConstant('{commonappdata}\OpenSeek'));
  end;
end;
