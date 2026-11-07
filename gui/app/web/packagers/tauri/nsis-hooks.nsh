!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    ; Remove what the app's setup installed (for WGSExtract: its CLI runtime, and the Pixi and MSYS2 it
    ; installed), with the bundle's own uninstall steps, as the app's uninstall runs them. The workspace,
    ; with the user's libraries and outputs, goes only if the user says so; a silent uninstall keeps it.
    ; Not while the app runs (Tauri checks this again right after the hook).
    !insertmacro CheckIfAppIsRunning "$INSTDIR\${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
    nsExec::ExecToStack '"$INSTDIR\node\node.exe" "$INSTDIR\app\dist\web\src\server\bundle-uninstall-cli.js" --app-support-name "${BUNDLEID}" --print-workspace "$INSTDIR\examples\EmbeddedBundle"'
    Pop $0
    Pop $1
    StrCpy $2 ""
    ${If} $0 == 0
    ${AndIf} $1 != ""
      ${If} ${Cmd} `MessageBox MB_ICONQUESTION|MB_YESNO|MB_DEFBUTTON2 "Also delete ${PRODUCTNAME}'s workspace folder?$\r$\n$\r$\n$1$\r$\nThis deletes the Reference and Genome Libraries, your outputs, logs and settings kept there, including any shared with other users. Choose No to keep them." /SD IDNO IDYES`
        StrCpy $2 "--remove-workspace"
      ${EndIf}
    ${EndIf}
    DetailPrint "Removing what the app's setup installed..."
    nsExec::ExecToLog '"$INSTDIR\node\node.exe" "$INSTDIR\app\dist\web\src\server\bundle-uninstall-cli.js" --app-support-name "${BUNDLEID}" $2 "$INSTDIR\examples\EmbeddedBundle"'
    Pop $0
    ${If} $0 != 0
      DetailPrint "Removing what the app's setup installed failed ($0)."
      MessageBox MB_ICONEXCLAMATION|MB_OK "Some of what ${PRODUCTNAME}'s setup installed could not be removed. Click Show details for what failed." /SD IDOK
      SetErrorLevel 2
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    SetShellVarContext current

    Delete /REBOOTOK "$INSTDIR\uninstall.exe"
    RmDir /REBOOTOK "$INSTDIR"
    RmDir /REBOOTOK "$LOCALAPPDATA\${PRODUCTNAME}"
    RmDir /REBOOTOK "$LOCALAPPDATA\Programs\${PRODUCTNAME}"

    ${If} $DeleteAppDataCheckboxState = 1
      ; The WebUI backend keeps its settings, and once kept bundle workspaces, in %LOCALAPPDATA%\<identifier>.
      RmDir /r "$LOCALAPPDATA\${BUNDLEID}"
    ${EndIf}
  ${EndIf}
!macroend
