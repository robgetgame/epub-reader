# 打包成单文件 exe。在仓库目录用 PowerShell 跑：.\build.ps1
# 产物：dist\EpubReader.exe。数据不在 exe 旁边（在 %LOCALAPPDATA%\EpubReader\），换新 exe 直接覆盖即可。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv\Scripts\python.exe)) {
    py -3.13 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
}
.\.venv\Scripts\python.exe tests\run_all.py
if ($LASTEXITCODE -ne 0) { throw "测试没过，不打包" }
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name EpubReader `
    --collect-all customtkinter `
    --hidden-import pythoncom --hidden-import win32com.client --hidden-import pywintypes `
    main.py
Get-Item dist\EpubReader.exe | Select-Object Name, Length, LastWriteTime
