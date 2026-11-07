!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    ; Remove what the app's setup installed (for WGSExtract: its CLI runtime, and the Pixi and MSYS2 it
    ; installed), with the bundle's own uninstall steps, as the app's uninstall runs them. User data in
    ; the workspace stays unless "Delete the application data" is ticked (below).
    ; Not while the app runs (Tauri checks this again right after the hook).
    !insertmacro CheckIfAppIsRunning "$INSTDIR\${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
    DetailPrint "Removing what the app's setup installed..."
    nsExec::ExecToLog '"$INSTDIR\node\node.exe" "$INSTDIR\app\dist\web\src\server\bundle-uninstall-cli.js" --app-support-name "${BUNDLEID}" "$INSTDIR\examples\EmbeddedBundle"'
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
      ; The WebUI backend keeps bundle workspaces and state in %LOCALAPPDATA%\<identifier>.
      RmDir /r "$LOCALAPPDATA\${BUNDLEID}"
    ${EndIf}
  ${EndIf}
!macroend
