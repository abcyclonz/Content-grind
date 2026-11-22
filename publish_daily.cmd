@echo off
rem Daily upload of queued clips for the megadenigo channel.
rem Run it by hand, or from Windows Task Scheduler once a day after 13:30 IST (YouTube's daily reset).
cd /d C:\claude\Content-grind
set PYTHONIOENCODING=utf-8
echo ===== %date% %time% >> data\publish_daily.log
.venv\Scripts\python.exe -m network.cli run-stage publish --channel megadenigo >> data\publish_daily.log 2>&1
