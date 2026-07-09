@echo off
cd /d "C:\Users\Mark C Chomba\Desktop\strategy agent1"
:loop
echo %date% %time% Starting live_bot.py >> bot_output.log
python live_bot.py >> bot_output.log 2>&1
echo %date% %time% Bot exited, restarting in 5s... >> bot_output.log
timeout /t 5 /nobreak >nul
goto loop
