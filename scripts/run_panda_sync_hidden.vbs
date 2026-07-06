Set shell = CreateObject("Wscript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""& 'D:\SelfMadeTool\AutoRegister\gptimage\scripts\sync_accounts_delta_to_panda.ps1' *>> 'D:\SelfMadeTool\AutoRegister\gptimage\data\panda-sync-delta-task.log'; exit $LASTEXITCODE"""
shell.Run cmd, 0, True
