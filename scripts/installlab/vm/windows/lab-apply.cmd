@echo off
rem Install-lab base image: lay Windows down the way Setup does, from WinPE.
rem
rem Windows 11 24H2's new Setup host stalls headless in QEMU before it touches the
rem disk, so autounattend.xml runs this instead (from its windowsPE pass): GPT
rem layout, apply install.wim, put the answer file where first boot reads it
rem (specialize/oobeSystem), make the disk bootable, reboot.
setlocal

set "MEDIA=%~d0"
set "WIM="
for %%d in (C D E F G H I J K L) do if exist %%d:\sources\install.wim set "WIM=%%d:\sources\install.wim"
if not defined WIM (
    echo install.wim not found & exit /b 1
)

(
echo select disk 0
echo clean
echo convert gpt
echo create partition efi size=300
echo format quick fs=fat32 label=System
echo assign letter=S
echo create partition msr size=16
echo create partition primary
echo format quick fs=ntfs label=Windows
echo assign letter=W
) > X:\lab-diskpart.txt
diskpart /s X:\lab-diskpart.txt || exit /b 1

rem The lab ISO carries only the Pro edition (named "Windows 11 Professional").
dism /Apply-Image /ImageFile:%WIM% /Index:1 /ApplyDir:W:\ || exit /b 1
mkdir W:\Windows\Panther
copy /y %MEDIA%\autounattend.xml W:\Windows\Panther\unattend.xml || exit /b 1
bcdboot W:\Windows /s S: /f UEFI || exit /b 1
wpeutil reboot
