schtasks /create /tn "LinkMonitor_0830" /sc daily /st 08:30 /tr "\"C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python.exe\" \"H:\张申冉_工作文件\脚本_配置文件_源码\link_monitor.py\""
schtasks /create /tn "LinkMonitor_1730" /sc daily /st 17:30 /tr "\"C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python.exe\" \"H:\张申冉_工作文件\脚本_配置文件_源码\link_monitor.py\""

#C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python.exe\换成真实的python路径，cmd执行where python查询