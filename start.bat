@echo off
echo.
echo  =====================================================
echo   MediTune ^| Sonic Therapeutics Unit
echo   Ward 404 ^| Administering audio dependencies...
echo  =====================================================
echo.
"C:\Users\princ\AppData\Local\Programs\Python\Python313\python.exe" -m pip install flask flask-cors yt-dlp
echo.
echo  [OK] All compounds synthesized. Starting server...
echo  [OK] Open http://localhost:5000 in your browser
echo.
"C:\Users\princ\AppData\Local\Programs\Python\Python313\python.exe" app.py
pause
