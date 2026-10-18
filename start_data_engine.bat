@echo off
cd /d "%~dp0"
echo Starting Data Engine (paper trading, $5000 per asset, all Kraken USD pairs)...
python data_engine.py
pause
