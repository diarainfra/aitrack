@echo off
rem aitrack brauseri laiendus: topeltklopsa seda faili Exploreris.
rem Seadistab uhenduse, kopeerib laienduse kausta tee loikelauale ja avab laienduste lehe.
cd /d "%~dp0"
python aitrack.py browser setup
echo.
pause
